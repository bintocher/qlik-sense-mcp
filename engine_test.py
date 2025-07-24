#!/usr/bin/env python3
"""
Тестовый файл для отладки Engine API вызовов к Qlik Sense.
Используется для разработки и тестирования новых методов получения мастер-объектов, листов и визуализаций.
"""

import os
import json
import ssl
import websocket
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("engine_test.log")],
)
logger = logging.getLogger(__name__)


class QlikEngineTestClient:
    """Тестовый клиент для Engine API с детальным логированием."""

    def __init__(self):
        """Инициализация клиента с чтением конфигурации из .env."""
        logger.info("=== Инициализация QlikEngineTestClient ===")

        # Читаем конфигурацию из переменных окружения
        self.server_url = os.getenv("QLIK_SERVER_URL", "")
        self.user_directory = os.getenv("QLIK_USER_DIRECTORY", "")
        self.user_id = os.getenv("QLIK_USER_ID", "")
        self.client_cert_path = os.getenv("QLIK_CLIENT_CERT_PATH")
        self.client_key_path = os.getenv("QLIK_CLIENT_KEY_PATH")
        self.ca_cert_path = os.getenv("QLIK_CA_CERT_PATH")
        self.engine_port = int(os.getenv("QLIK_ENGINE_PORT", "4747"))
        self.verify_ssl = os.getenv("QLIK_VERIFY_SSL", "true").lower() == "true"

        # Логируем основную конфигурацию
        logger.info(
            f"Подключение к: {self.server_url}:{self.engine_port} как {self.user_id}@{self.user_directory}"
        )

        # Проверяем что все необходимые параметры заданы
        if not all([self.server_url, self.user_directory, self.user_id]):
            raise ValueError(
                "Отсутствуют обязательные переменные окружения: QLIK_SERVER_URL, QLIK_USER_DIRECTORY, QLIK_USER_ID"
            )

        self.ws = None
        self.request_id = 0
        self.app_handle = -1
        self.current_app_id = None

        logger.info("Инициализация завершена успешно")

    def _get_next_request_id(self) -> int:
        """Получить следующий ID запроса."""
        self.request_id += 1
        return self.request_id

    def connect(self, app_id: str = None) -> bool:
        """Подключение к Engine API через WebSocket."""
        server_host = self.server_url.replace("https://", "").replace("http://", "")

        # Если передан app_id - подключаемся к конкретному приложению
        if app_id:
            endpoints_to_try = [
                f"wss://{server_host}:{self.engine_port}/app/{app_id}",
                f"ws://{server_host}:{self.engine_port}/app/{app_id}",
            ]
            self.current_app_id = app_id
        else:
            # Общие эндпоинты для операций без конкретного приложения
            endpoints_to_try = [
                f"wss://{server_host}:{self.engine_port}/app/engineData",
                f"wss://{server_host}:{self.engine_port}/app",
                f"ws://{server_host}:{self.engine_port}/app/engineData",
                f"ws://{server_host}:{self.engine_port}/app",
            ]
            self.current_app_id = None

        # Настройка SSL контекста
        ssl_context = ssl.create_default_context()
        if not self.verify_ssl:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        if self.client_cert_path and self.client_key_path:
            try:
                ssl_context.load_cert_chain(self.client_cert_path, self.client_key_path)
            except Exception as e:
                logger.error(f"Ошибка загрузки клиентских сертификатов: {e}")
                return False

        if self.ca_cert_path:
            try:
                ssl_context.load_verify_locations(self.ca_cert_path)
            except Exception as e:
                logger.error(f"Ошибка загрузки CA сертификата: {e}")
                return False

        # Заголовки для аутентификации
        headers = [
            f"X-Qlik-User: UserDirectory={self.user_directory}; UserId={self.user_id}"
        ]

        # Пробуем подключиться к каждому эндпоинту
        last_error = None
        for i, url in enumerate(endpoints_to_try, 1):
            try:
                if url.startswith("wss://"):
                    self.ws = websocket.create_connection(
                        url, sslopt={"context": ssl_context}, header=headers, timeout=10
                    )
                else:
                    self.ws = websocket.create_connection(
                        url, header=headers, timeout=10
                    )

                # Получаем первое сообщение от сервера
                initial_message = self.ws.recv()
                return True

            except Exception as e:
                last_error = e
                if self.ws:
                    self.ws.close()
                    self.ws = None
                continue

        logger.error(
            f"Не удалось подключиться ни к одному эндпоинту. Последняя ошибка: {last_error}"
        )
        return False

    def disconnect(self) -> None:
        """Отключение от Engine API."""
        if self.ws:
            try:
                self.ws.close()
            except Exception as e:
                logger.warning(f"Ошибка при закрытии WebSocket: {e}")
            self.ws = None
        self.app_handle = -1
        self.current_app_id = None

    def send_request(
        self, method: str, params: List[Any] = None, handle: int = -1
    ) -> Dict[str, Any]:
        """Отправка JSON-RPC запроса к Engine API."""
        if not self.ws:
            logger.error("WebSocket соединение не установлено")
            return {"error": "No connection"}

        if params is None:
            params = []

        request_id = self._get_next_request_id()

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "handle": handle,
            "params": params,
        }

        try:
            self.ws.send(json.dumps(request))
            response_text = self.ws.recv()
            response = json.loads(response_text)

            if "error" in response:
                logger.error(f"Ошибка в ответе: {response['error']}")

            return response

        except Exception as e:
            logger.error(f"Ошибка при отправке запроса: {e}")
            return {"error": str(e)}

    def open_app(self, app_id: str) -> Dict[str, Any]:
        """Открытие приложения (в контексте соединения с конкретным app_id)."""
        # Если уже подключены к другому приложению - отключаемся
        if self.current_app_id and self.current_app_id != app_id:
            self.disconnect()

        # Подключаемся к конкретному приложению
        if not self.ws or self.current_app_id != app_id:
            if not self.connect(app_id):
                return {"error": "Failed to connect to app"}

        # Открываем документ в контексте этого приложения
        response = self.send_request("OpenDoc", [app_id], handle=-1)

        if "result" in response and "qReturn" in response["result"]:
            self.app_handle = response["result"]["qReturn"]["qHandle"]
        else:
            logger.error(f"❌ Ошибка открытия приложения: {response}")

        return response

    def get_doc_list(self) -> Dict[str, Any]:
        """Получение списка документов (через общее соединение)."""
        logger.info("=== Получение списка документов ===")

        # Отключаемся от конкретного приложения если подключены
        if self.current_app_id:
            self.disconnect()

        # Подключаемся к общему эндпоинту
        if not self.connect():
            return {"error": "Failed to connect to engine"}

        return self.send_request("GetDocList", [], handle=-1)

    def create_sheet_list_object(self, doc_handle: int) -> Dict[str, Any]:
        """Создание SessionObject для получения списка листов."""
        sheet_list_def = {
            "qInfo": {"qType": "SheetList"},
            "qAppObjectListDef": {
                "qType": "sheet",
                "qData": {
                    "title": "/qMetaDef/title",
                    "description": "/qMetaDef/description",
                    "thumbnail": "/thumbnail",
                    "cells": "/cells",
                    "rank": "/rank",
                    "columns": "/columns",
                    "rows": "/rows",
                },
            },
        }

        response = self.send_request(
            "CreateSessionObject", [sheet_list_def], handle=doc_handle
        )

        if "result" not in response or "qReturn" not in response["result"]:
            logger.error(f"❌ Ошибка создания SheetList объекта: {response}")

        return response

    def create_measure_list_object(self, doc_handle: int) -> Dict[str, Any]:
        """Создание объекта MeasureList для получения мастер-мер."""
        request_data = {
            "qInfo": {
                "qType": "MeasureList"
            },
            "qMeasureListDef": {
                "qType": "measure",
                "qData": {
                    "title": "/title",
                    "tags": "/tags",
                    "description": "/qMeta/description",
                    "expression": "/qMeasure/qDef"
                }
            }
        }

        response = self.send_request("CreateSessionObject", [request_data], handle=doc_handle)

        if "result" not in response or "qReturn" not in response["result"]:
            logger.error(f"❌ Ошибка создания MeasureList: {response}")

        return response

    def create_dimension_list_object(self, doc_handle: int) -> Dict[str, Any]:
        """Создание объекта DimensionList для получения мастер-измерений."""
        request_data = {
            "qInfo": {
                "qType": "DimensionList"
            },
            "qDimensionListDef": {
                "qType": "dimension",
                "qData": {
                    "title": "/title",
                    "tags": "/tags",
                    "grouping": "/qDim/qGrouping",
                    "info": "/qDimInfos",
                    "description": "/qMeta/description",
                    "expression": "/qDim/qFieldDefs"
                }
            }
        }

        response = self.send_request("CreateSessionObject", [request_data], handle=doc_handle)

        if "result" not in response or "qReturn" not in response["result"]:
            logger.error(f"❌ Ошибка создания DimensionList: {response}")

        return response

    def get_master_measures(self, app_id: str) -> Dict[str, Any]:
        """Получение всех мастер-мер приложения."""
        logger.info(f"=== Получение мастер-мер приложения {app_id} ===")

        # Открываем приложение
        if self.current_app_id != app_id:
            self.open_app(app_id)

        # Создаем объект MeasureList
        measure_list_response = self.create_measure_list_object(self.app_handle)
        if "error" in measure_list_response:
            return {"error": f"Failed to create MeasureList: {measure_list_response}"}

        measure_list_handle = measure_list_response["result"]["qReturn"]["qHandle"]

        # Получаем layout с данными
        layout_response = self.get_layout(measure_list_handle)
        if "error" in layout_response:
            return {"error": f"Failed to get MeasureList layout: {layout_response}"}

        layout = layout_response.get("result", {}).get("qLayout", {})
        measure_list = layout.get("qMeasureList", {})
        measures = measure_list.get("qItems", [])

        logger.info(f"✅ Найдено {len(measures)} мастер-мер")

        result = {
            "measures": measures,
            "count": len(measures)
        }

        return result

    def get_master_dimensions(self, app_id: str) -> Dict[str, Any]:
        """Получение всех мастер-измерений приложения."""
        logger.info(f"=== Получение мастер-измерений приложения {app_id} ===")

        # Открываем приложение
        if self.current_app_id != app_id:
            self.open_app(app_id)

        # Создаем объект DimensionList
        dimension_list_response = self.create_dimension_list_object(self.app_handle)
        if "error" in dimension_list_response:
            return {"error": f"Failed to create DimensionList: {dimension_list_response}"}

        dimension_list_handle = dimension_list_response["result"]["qReturn"]["qHandle"]

        # Получаем layout с данными
        layout_response = self.get_layout(dimension_list_handle)
        if "error" in layout_response:
            return {"error": f"Failed to get DimensionList layout: {layout_response}"}

        layout = layout_response.get("result", {}).get("qLayout", {})
        dimension_list = layout.get("qDimensionList", {})
        dimensions = dimension_list.get("qItems", [])

        logger.info(f"✅ Найдено {len(dimensions)} мастер-измерений")

        result = {
            "dimensions": dimensions,
            "count": len(dimensions)
        }

        return result

    def analyze_master_items(self, app_id: str) -> Dict[str, Any]:
        """Полный анализ мастер-мер и мастер-измерений приложения."""
        logger.info(f"=== АНАЛИЗ МАСТЕР-ЭЛЕМЕНТОВ ПРИЛОЖЕНИЯ {app_id} ===")

        result = {
            "measures": [],
            "dimensions": [],
            "summary": {}
        }

        # Получаем мастер-меры
        measures_result = self.get_master_measures(app_id)
        if "error" not in measures_result:
            measures = measures_result.get("measures", [])
            result["measures"] = measures

            logger.info(f"📏 Анализ мастер-мер ({len(measures)}):")
            for i, measure in enumerate(measures, 1):
                info = measure.get("qInfo", {})
                meta = measure.get("qMeta", {})
                data = measure.get("qData", {})

                title = meta.get("title", "Без названия")
                description = meta.get("description", "")
                measure_def = measure.get("qMeasure", {}).get("qDef", "")

                logger.info(f"  {i}. {title}")
                if description:
                    logger.info(f"     📝 Описание: {description}")
                if measure_def:
                    logger.info(f"     🧮 Формула: {measure_def}")

                # Дополнительная информация
                created = meta.get("createdDate", "")
                modified = meta.get("modifiedDate", "")
                published = meta.get("published", False)
                if created:
                    logger.info(f"     📅 Создана: {created}")
                if published:
                    logger.info(f"     ✅ Опубликована")

        # Получаем мастер-измерения
        dimensions_result = self.get_master_dimensions(app_id)
        if "error" not in dimensions_result:
            dimensions = dimensions_result.get("dimensions", [])
            result["dimensions"] = dimensions

            logger.info(f"📐 Анализ мастер-измерений ({len(dimensions)}):")
            for i, dimension in enumerate(dimensions, 1):
                info = dimension.get("qInfo", {})
                meta = dimension.get("qMeta", {})
                data = dimension.get("qData", {})

                title = meta.get("title", "Без названия")
                description = meta.get("description", "")
                dim_def = dimension.get("qDim", {})
                field_defs = dim_def.get("qFieldDefs", [])

                logger.info(f"  {i}. {title}")
                if description:
                    logger.info(f"     📝 Описание: {description}")
                if field_defs:
                    logger.info(f"     🏷️ Поля: {', '.join(field_defs)}")

                # Дополнительная информация
                created = meta.get("createdDate", "")
                modified = meta.get("modifiedDate", "")
                published = meta.get("published", False)
                if created:
                    logger.info(f"     📅 Создано: {created}")
                if published:
                    logger.info(f"     ✅ Опубликовано")

        # Сводка
        result["summary"] = {
            "total_measures": len(result["measures"]),
            "total_dimensions": len(result["dimensions"]),
            "published_measures": sum(1 for m in result["measures"] if m.get("qMeta", {}).get("published", False)),
            "published_dimensions": sum(1 for d in result["dimensions"] if d.get("qMeta", {}).get("published", False))
        }

        summary = result["summary"]
        logger.info(f"📊 Сводка мастер-элементов:")
        logger.info(f"  📏 Мастер-меры: {summary['total_measures']} (опубликовано: {summary['published_measures']})")
        logger.info(f"  📐 Мастер-измерения: {summary['total_dimensions']} (опубликовано: {summary['published_dimensions']})")

        return result

    def create_variable_list_object(self, doc_handle: int) -> Dict[str, Any]:
        """Создание объекта VariableList для получения переменных."""
        request_data = {
            "qInfo": {
                "qType": "VariableList"
            },
            "qVariableListDef": {
                "qType": "variable",
                "qShowReserved": True,
                "qShowConfig": True,
                "qData": {
                    "tags": "/tags"
                }
            }
        }

        response = self.send_request("CreateSessionObject", [request_data], handle=doc_handle)

        if "result" not in response or "qReturn" not in response["result"]:
            logger.error(f"❌ Ошибка создания VariableList: {response}")

        return response

    def get_variables(self, app_id: str) -> Dict[str, Any]:
        """Получение всех переменных приложения."""
        logger.info(f"=== Получение переменных приложения {app_id} ===")

        # Открываем приложение
        if self.current_app_id != app_id:
            self.open_app(app_id)

        # Создаем объект VariableList
        variable_list_response = self.create_variable_list_object(self.app_handle)
        if "error" in variable_list_response:
            return {"error": f"Failed to create VariableList: {variable_list_response}"}

        variable_list_handle = variable_list_response["result"]["qReturn"]["qHandle"]

        # Получаем layout с данными
        layout_response = self.get_layout(variable_list_handle)
        if "error" in layout_response:
            return {"error": f"Failed to get VariableList layout: {layout_response}"}

        layout = layout_response.get("result", {}).get("qLayout", {})
        variable_list = layout.get("qVariableList", {})
        variables = variable_list.get("qItems", [])

        logger.info(f"✅ Найдено {len(variables)} переменных")

        result = {
            "variables": variables,
            "count": len(variables)
        }

        return result

    def get_variable_by_id(self, app_id: str, variable_id: str) -> Dict[str, Any]:
        """Получение конкретной переменной по ID."""
        # Открываем приложение
        if self.current_app_id != app_id:
            self.open_app(app_id)

        # Получаем переменную
        response = self.send_request("GetVariableById", {"qId": variable_id}, handle=self.app_handle)

        if "result" not in response or "qReturn" not in response["result"]:
            logger.error(f"❌ Ошибка получения переменной {variable_id}: {response}")
            return {"error": f"Failed to get variable: {response}"}

        return response

    def get_variable_value(self, app_id: str, variable_id: str) -> Dict[str, Any]:
        """Получение значения переменной."""
        # Получаем переменную
        variable_response = self.get_variable_by_id(app_id, variable_id)
        if "error" in variable_response:
            return variable_response

        variable_handle = variable_response["result"]["qReturn"]["qHandle"]

        # Получаем layout с текущим значением
        layout_response = self.get_layout(variable_handle)
        if "error" in layout_response:
            return {"error": f"Failed to get variable value layout: {layout_response}"}

        layout = layout_response.get("result", {}).get("qLayout", {})

        result = {
            "qText": layout.get("qText", ""),
            "qNum": layout.get("qNum", None),
            "qIsScriptCreated": layout.get("qIsScriptCreated", False),
            "info": layout.get("qInfo", {}),
            "meta": layout.get("qMeta", {})
        }

        return result

    def analyze_variables(self, app_id: str) -> Dict[str, Any]:
        """Полный анализ всех переменных приложения."""
        logger.info(f"=== АНАЛИЗ ПЕРЕМЕННЫХ ПРИЛОЖЕНИЯ {app_id} ===")

        result = {
            "variables": [],
            "user_variables": [],
            "system_variables": [],
            "script_variables": [],
            "summary": {}
        }

        # Получаем все переменные
        variables_result = self.get_variables(app_id)
        if "error" in variables_result:
            logger.error(f"❌ Ошибка получения переменных: {variables_result}")
            return variables_result

        variables = variables_result.get("variables", [])
        result["variables"] = variables

        if not variables:
            logger.info("📝 Переменные в приложении не найдены")
            return result

        logger.info(f"📝 Анализ переменных ({len(variables)}):")

        for i, variable in enumerate(variables, 1):
            var_name = variable.get("qName", "")
            var_definition = variable.get("qDefinition", "")
            var_id = variable.get("qInfo", {}).get("qId", "")
            is_reserved = variable.get("qIsReserved", False)
            is_script_created = variable.get("qIsScriptCreated", False)

            logger.info(f"  {i}. {var_name}")
            if var_definition:
                logger.info(f"     🧮 Определение: {var_definition}")

            # Получаем текущее значение переменной
            if var_id:
                value_result = self.get_variable_value(app_id, var_id)
                if "error" not in value_result:
                    qtext = value_result.get("qText", "")
                    qnum = value_result.get("qNum", None)

                    if qtext:
                        logger.info(f"     💾 Значение (текст): {qtext}")
                    if qnum is not None:
                        logger.info(f"     🔢 Значение (число): {qnum}")

            # Дополнительная информация
            if is_reserved:
                logger.info(f"     🔒 Системная переменная")
                result["system_variables"].append(variable)
            else:
                result["user_variables"].append(variable)

            if is_script_created:
                logger.info(f"     📜 Создана в скрипте")
                result["script_variables"].append(variable)

        # Сводка
        result["summary"] = {
            "total_variables": len(variables),
            "user_variables": len(result["user_variables"]),
            "system_variables": len(result["system_variables"]),
            "script_variables": len(result["script_variables"])
        }

        summary = result["summary"]
        logger.info(f"📊 Сводка переменных:")
        logger.info(f"  📝 Всего переменных: {summary['total_variables']}")
        logger.info(f"  👤 Пользовательские: {summary['user_variables']}")
        logger.info(f"  🔒 Системные: {summary['system_variables']}")
        logger.info(f"  📜 Из скрипта: {summary['script_variables']}")

        return result

    def get_layout(self, object_handle: int) -> Dict[str, Any]:
        """Получение layout объекта по handle."""
        response = self.send_request("GetLayout", [], handle=object_handle)

        if "result" not in response or "qLayout" not in response["result"]:
            logger.error(f"❌ Ошибка получения layout для handle {object_handle}: {response}")

        return response

    def get_sheets_with_objects(self, app_id: str) -> Dict[str, Any]:
        """Получение всех листов приложения с объектами."""
        logger.info(f"=== Получение листов и объектов приложения {app_id} ===")

        # Открываем приложение если не открыто
        if not self.ws or self.current_app_id != app_id:
            open_response = self.open_app(app_id)
            if "error" in open_response:
                return open_response

        # Создаем объект SheetList
        sheet_list_response = self.create_sheet_list_object(self.app_handle)
        if "error" in sheet_list_response:
            return sheet_list_response

        sheet_list_handle = sheet_list_response["result"]["qReturn"]["qHandle"]

        # Получаем layout с данными о листах
        layout_response = self.get_layout(sheet_list_handle)
        if "error" in layout_response:
            return layout_response

        # Извлекаем информацию о листах
        if "result" in layout_response and "qLayout" in layout_response["result"]:
            layout = layout_response["result"]["qLayout"]
            if "qAppObjectList" in layout and "qItems" in layout["qAppObjectList"]:
                sheets = layout["qAppObjectList"]["qItems"]
                logger.info(f"✅ Найдено {len(sheets)} листов")

                # Обрабатываем каждый лист и извлекаем объекты из cells
                processed_sheets = []
                total_objects = 0

                for i, sheet in enumerate(sheets, 1):
                    title = sheet.get("qMeta", {}).get("title", "Без названия")
                    sheet_id = sheet.get("qInfo", {}).get("qId", "Неизвестно")

                    # Извлекаем объекты (cells) из данных листа
                    cells = sheet.get("qData", {}).get("cells", [])

                    logger.info(f"  {i}. {title} (ID: {sheet_id}) - {len(cells)} объектов")

                    # Логируем детали объектов если они есть
                    if cells:
                        for j, cell in enumerate(cells, 1):
                            obj_name = cell.get("name", "Неизвестно")
                            obj_type = cell.get("type", "Неизвестно")
                            logger.info(f"    {j}. {obj_name} ({obj_type})")

                    # Добавляем обработанную информацию о листе
                    processed_sheet = {
                        "sheet_info": sheet,
                        "sheet_id": sheet_id,
                        "title": title,
                        "objects": cells,
                        "objects_count": len(cells),
                    }
                    processed_sheets.append(processed_sheet)
                    total_objects += len(cells)

                logger.info(f"📊 Итого объектов на всех листах: {total_objects}")

                return {
                    "sheets": processed_sheets,
                    "total_sheets": len(sheets),
                    "total_objects": total_objects,
                }
            else:
                logger.warning("❌ В ответе нет данных о листах")
                return {"error": "No sheets data in response"}
        else:
            logger.error("❌ Некорректный ответ layout")
            return {"error": "Invalid layout response"}

    def create_sheet_object_list(self, doc_handle: int, sheet_id: str) -> Dict[str, Any]:
        """Создание SessionObject для получения объектов конкретного листа."""
        logger.info(f"=== Создание объекта для получения объектов листа {sheet_id} ===")

        object_list_def = {
            "qInfo": {"qType": "SheetObjectList"},
            "qAppObjectListDef": {
                "qType": "visualization",
                "qFilter": f"qParent eq '{sheet_id}'",
                "qData": {
                    "title": "/qMetaDef/title",
                    "description": "/qMetaDef/description",
                    "objectType": "/qInfo/qType",
                    "visualization": "/visualization",
                    "showTitles": "/showTitles",
                },
            },
        }

        response = self.send_request(
            "CreateSessionObject", [object_list_def], handle=doc_handle
        )

        if "result" in response and "qReturn" in response["result"]:
            object_list_handle = response["result"]["qReturn"]["qHandle"]
            logger.info(f"✅ SheetObjectList создан, handle: {object_list_handle}")
        else:
            logger.error(f"❌ Ошибка создания SheetObjectList: {response}")

        return response

    def get_sheet_objects(self, app_id: str, sheet_id: str) -> Dict[str, Any]:
        """Получение всех объектов конкретного листа."""
        logger.info(
            f"=== Получение объектов листа {sheet_id} в приложении {app_id} ==="
        )

        # Открываем приложение если не открыто
        if not self.ws or self.current_app_id != app_id:
            open_response = self.open_app(app_id)
            if "error" in open_response:
                return open_response

        # Создаем объект SheetObjectList
        object_list_response = self.create_sheet_object_list(self.app_handle, sheet_id)
        if "error" in object_list_response:
            return object_list_response

        object_list_handle = object_list_response["result"]["qReturn"]["qHandle"]

        # Получаем layout с данными об объектах
        layout_response = self.get_layout(object_list_handle)
        if "error" in layout_response:
            return layout_response

        # Извлекаем информацию об объектах
        if "result" in layout_response and "qLayout" in layout_response["result"]:
            layout = layout_response["result"]["qLayout"]
            if "qAppObjectList" in layout and "qItems" in layout["qAppObjectList"]:
                objects = layout["qAppObjectList"]["qItems"]
                logger.info(f"✅ Найдено {len(objects)} объектов на листе")

                # Логируем краткую информацию о каждом объекте
                for i, obj in enumerate(objects, 1):
                    title = obj.get("qMeta", {}).get("title", "Без названия")
                    obj_type = obj.get("qData", {}).get("objectType", "Неизвестно")
                    obj_id = obj.get("qInfo", {}).get("qId", "Неизвестно")
                    logger.info(f"  {i}. {title} ({obj_type}, ID: {obj_id})")

                return {"objects": objects, "total_count": len(objects)}
            else:
                logger.warning("❌ В ответе нет данных об объектах")
                return {"error": "No objects data in response"}
        else:
            logger.error("❌ Некорректный ответ layout")
            return {"error": "Invalid layout response"}

    def get_object(self, app_id: str, object_id: str) -> Dict[str, Any]:
        """Получение объекта по ID."""
        # Открываем приложение если не открыто
        if not self.ws or self.current_app_id != app_id:
            open_response = self.open_app(app_id)
            if "error" in open_response:
                return open_response

        # Получаем объект по ID
        response = self.send_request("GetObject", {"qId": object_id}, handle=self.app_handle)

        if "result" in response and "qReturn" in response["result"]:
            object_handle = response["result"]["qReturn"]["qHandle"]
            object_type = response["result"]["qReturn"]["qGenericType"]
        else:
            logger.error(f"❌ Ошибка получения объекта {object_id}: {response}")

        return response

    def get_object_properties(self, object_handle: int) -> Dict[str, Any]:
        """Получение свойств объекта по handle."""
        response = self.send_request("GetProperties", [], handle=object_handle)

        if "result" not in response or "qProp" not in response["result"]:
            logger.error(f"❌ Ошибка получения свойств для handle {object_handle}: {response}")

        return response

    def analyze_object(self, app_id: str, object_id: str, object_name: str = None) -> Dict[str, Any]:
        """Комплексный анализ объекта: получение handle, layout и properties."""
        display_name = object_name or object_id
        logger.info(f"=== АНАЛИЗ ОБЪЕКТА: {display_name} ({object_id}) ===")

        # Получаем объект
        object_response = self.get_object(app_id, object_id)
        if "error" in object_response:
            return {"error": f"Failed to get object: {object_response}"}

        object_handle = object_response["result"]["qReturn"]["qHandle"]
        object_type = object_response["result"]["qReturn"]["qGenericType"]

        # Получаем layout
        layout_response = self.get_layout(object_handle)
        if "error" in layout_response:
            return {"error": f"Failed to get layout: {layout_response}"}

        # Получаем properties
        properties_response = self.get_object_properties(object_handle)
        if "error" in properties_response:
            return {"error": f"Failed to get properties: {properties_response}"}

        # Анализируем данные
        layout = layout_response.get("result", {}).get("qLayout", {})
        properties = properties_response.get("result", {}).get("qProp", {})

        # Извлекаем основную информацию
        title = properties.get("qMetaDef", {}).get("title", "Без названия")
        description = properties.get("qMetaDef", {}).get("description", "")

        logger.info(f"📊 Тип: {object_type}")
        logger.info(f"📊 Название: {title}")
        if description:
            logger.info(f"📊 Описание: {description}")

        # Анализируем меры
        measures = self._extract_measures(properties)
        if measures:
            logger.info(f"📏 Меры ({len(measures)}):")
            for i, measure in enumerate(measures, 1):
                label = measure.get("qDef", {}).get("qLabel", "Без названия")
                expression = measure.get("qDef", {}).get("qDef", "")
                logger.info(f"  {i}. {label}: {expression}")

        # Анализируем измерения
        dimensions = self._extract_dimensions(properties)
        if dimensions:
            logger.info(f"📐 Измерения ({len(dimensions)}):")
            for i, dimension in enumerate(dimensions, 1):
                label = dimension.get("qDef", {}).get("qLabel", "Без названия")
                field = dimension.get("qDef", {}).get("qFieldDefs", [""])[0] if dimension.get("qDef", {}).get("qFieldDefs") else ""
                logger.info(f"  {i}. {label}: {field}")

        # Анализируем данные объекта
        data_info = self._extract_object_data(layout)
        if data_info:
            logger.info(f"💾 Данные объекта:")
            if data_info.get("values"):
                logger.info(f"  📊 Значения: {data_info['values']}")
            if data_info.get("matrix_info"):
                logger.info(f"  📋 Матрица: {data_info['matrix_info']}")

            # Показываем реальные данные
            self._log_object_data(data_info)
        else:
            # Если данные не найдены, показываем отладочную информацию
            logger.info(f"⚠️ Данные не найдены, проверяем структуру layout:")
            self._debug_layout_structure(layout, object_type)

        return {
            "object_id": object_id,
            "handle": object_handle,
            "type": object_type,
            "title": title,
            "description": description,
            "measures": measures,
            "dimensions": dimensions,
            "data_info": data_info,
            "layout": layout,
            "properties": properties
        }

    def _extract_measures(self, properties: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Извлечение мер из свойств объекта."""
        measures = []

        # Ищем меры в разных местах в зависимости от типа объекта
        # qHyperCubeDef для стандартных визуализаций
        hypercube = properties.get("qHyperCubeDef", {})
        if "qMeasures" in hypercube:
            measures.extend(hypercube["qMeasures"])

        # qListObjectDef для других объектов
        listobj = properties.get("qListObjectDef", {})
        if "qMeasures" in listobj:
            measures.extend(listobj["qMeasures"])

        # Специфичные места для KPI
        if "qMeasure" in properties:
            measures.append(properties["qMeasure"])

        return measures

    def _extract_dimensions(self, properties: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Извлечение измерений из свойств объекта."""
        dimensions = []

        # Ищем измерения в разных местах
        # qHyperCubeDef для стандартных визуализаций
        hypercube = properties.get("qHyperCubeDef", {})
        if "qDimensions" in hypercube:
            dimensions.extend(hypercube["qDimensions"])

        # qListObjectDef для других объектов
        listobj = properties.get("qListObjectDef", {})
        if "qDimensions" in listobj:
            dimensions.extend(listobj["qDimensions"])

        return dimensions

    def _extract_object_data(self, layout: Dict[str, Any]) -> Dict[str, Any]:
        """Извлечение данных объекта из layout (qText, qNum значения)."""
        data_info = {}

        # Универсальный поиск данных во всем layout
        matrix_data = []
        list_values = []
        simple_values = []

        self._find_data_recursive(layout, matrix_data, list_values, simple_values)

        # Формируем результат
        if matrix_data:
            total_cells = sum(len(row) for row in matrix_data)
            data_info["matrix_data"] = matrix_data
            data_info["matrix_info"] = f"{len(matrix_data)} строк, {total_cells} ячеек"

        if list_values:
            data_info["list_values"] = list_values
            data_info["values"] = f"{len(list_values)} значений"

        if simple_values:
            data_info["simple_values"] = simple_values

        return data_info

    def _find_data_recursive(self, obj: any, matrix_data: list, list_values: list, simple_values: list, path: str = "") -> None:
        """Рекурсивный поиск qText/qNum данных в любом объекте."""
        if isinstance(obj, dict):
            # Если это ячейка с данными
            if "qText" in obj or "qNum" in obj:
                qtext = obj.get("qText", "")
                qnum = obj.get("qNum", None)
                if qtext or qnum is not None:
                    data_item = {
                        "qText": qtext,
                        "qNum": qnum,
                        "qState": obj.get("qState", ""),
                        "qElemNumber": obj.get("qElemNumber", ""),
                        "field": path
                    }
                    simple_values.append(data_item)

            # Если это матрица данных
            if "qMatrix" in obj:
                matrix = obj["qMatrix"]
                if isinstance(matrix, list):
                    for row in matrix:
                        if isinstance(row, list):
                            row_data = []
                            for cell in row:
                                if isinstance(cell, dict) and ("qText" in cell or "qNum" in cell):
                                    qtext = cell.get("qText", "")
                                    qnum = cell.get("qNum", None)
                                    if qtext or qnum is not None:
                                        row_data.append({
                                            "qText": qtext,
                                            "qNum": qnum,
                                            "qState": cell.get("qState", ""),
                                            "qElemNumber": cell.get("qElemNumber", "")
                                        })
                            if row_data:
                                matrix_data.append(row_data)
                        elif isinstance(row, dict) and ("qText" in row or "qNum" in row):
                            # Одиночные значения в матрице
                            qtext = row.get("qText", "")
                            qnum = row.get("qNum", None)
                            if qtext or qnum is not None:
                                list_values.append({
                                    "qText": qtext,
                                    "qNum": qnum,
                                    "qState": row.get("qState", ""),
                                    "qElemNumber": row.get("qElemNumber", ""),
                                    "field": path
                                })

            # Рекурсивно обходим все ключи
            for key, value in obj.items():
                if key not in ["qInfo", "qMeta", "qSelectionInfo"]:  # Пропускаем служебные поля
                    new_path = f"{path}.{key}" if path else key
                    self._find_data_recursive(value, matrix_data, list_values, simple_values, new_path)

        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                new_path = f"{path}[{i}]" if path else f"[{i}]"
                self._find_data_recursive(item, matrix_data, list_values, simple_values, new_path)

    def _log_object_data(self, data_info: Dict[str, Any]) -> None:
        """Логирование реальных данных объекта (qText, qNum значения)."""

        # Выводим данные матрицы (таблицы, графики)
        if "matrix_data" in data_info:
            matrix_data = data_info["matrix_data"]
            logger.info(f"  🔢 Данные матрицы:")

            for i, row in enumerate(matrix_data[:5], 1):  # Показываем первые 5 строк
                row_values = []
                for cell in row:
                    qtext = cell.get("qText", "")
                    qnum = cell.get("qNum", None)

                    if qnum is not None:
                        row_values.append(f"{qtext} ({qnum})")
                    else:
                        row_values.append(qtext)

                logger.info(f"    {i}. {' | '.join(row_values)}")

            if len(matrix_data) > 5:
                logger.info(f"    ... и еще {len(matrix_data) - 5} строк")

        # Выводим данные списка (фильтры, селекторы)
        elif "list_values" in data_info:
            list_values = data_info["list_values"]
            logger.info(f"  📋 Значения списка:")

            for i, value in enumerate(list_values[:10], 1):  # Показываем первые 10 значений
                qtext = value.get("qText", "")
                qnum = value.get("qNum", None)
                qstate = value.get("qState", "")

                if qnum is not None:
                    logger.info(f"    {i}. {qtext} ({qnum}) [{qstate}]")
                else:
                    logger.info(f"    {i}. {qtext} [{qstate}]")

            if len(list_values) > 10:
                logger.info(f"    ... и еще {len(list_values) - 10} значений")

        # Выводим простые значения (KPI)
        elif "simple_values" in data_info:
            simple_values = data_info["simple_values"]
            logger.info(f"  💡 Простые значения:")

            for value in simple_values:
                field = value.get("field", "")
                qtext = value.get("qText", "")
                qnum = value.get("qNum", None)

                if qnum is not None:
                    logger.info(f"    {field}: {qtext} ({qnum})")
                else:
                    logger.info(f"    {field}: {qtext}")

    def _debug_layout_structure(self, layout: Dict[str, Any], object_type: str) -> None:
        """Отладочная информация о структуре layout."""
        logger.info(f"  🔍 Ключи layout для {object_type}: {list(layout.keys())}")

        # Ищем потенциальные места с данными
        potential_data_keys = []
        for key, value in layout.items():
            if isinstance(value, dict):
                if any(sub_key in value for sub_key in ["qDataPages", "qMatrix", "qText", "qNum"]):
                    potential_data_keys.append(key)
                # Также проверяем вложенные структуры
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, dict) and any(data_key in sub_value for data_key in ["qDataPages", "qMatrix"]):
                        potential_data_keys.append(f"{key}.{sub_key}")

        if potential_data_keys:
            logger.info(f"  📋 Потенциальные источники данных: {potential_data_keys}")

            # Показываем детали первого найденного источника
            first_key = potential_data_keys[0]
            if "." in first_key:
                main_key, sub_key = first_key.split(".", 1)
                data_source = layout.get(main_key, {}).get(sub_key, {})
            else:
                data_source = layout.get(first_key, {})

            if "qDataPages" in data_source:
                pages = data_source["qDataPages"]
                logger.info(f"  📄 qDataPages: {len(pages)} страниц")
                if pages and "qMatrix" in pages[0]:
                    matrix = pages[0]["qMatrix"]
                    logger.info(f"  📊 qMatrix: {len(matrix)} строк")
                    if matrix:
                        logger.info(f"  📝 Первая строка: {matrix[0]}")
        else:
            logger.info(f"  ❌ Не найдено потенциальных источников данных")
            # Показываем несколько первых ключей для понимания структуры
            sample_keys = list(layout.keys())[:5]
            for key in sample_keys:
                value = layout[key]
                if isinstance(value, dict):
                    logger.info(f"  📁 {key}: {list(value.keys())[:3]}...")
                else:
                    logger.info(f"  📝 {key}: {type(value).__name__}")

    def analyze_all_objects(self, app_id: str, limit_objects: int = None) -> Dict[str, Any]:
        """Анализ всех объектов приложения с детальной информацией."""
        logger.info(f"=== ПОЛНЫЙ АНАЛИЗ ОБЪЕКТОВ ПРИЛОЖЕНИЯ {app_id} ===")

        # Получаем листы с объектами
        sheets_response = self.get_sheets_with_objects(app_id)
        if "error" in sheets_response:
            return sheets_response

        sheets = sheets_response.get("sheets", [])
        total_objects = sheets_response.get("total_objects", 0)

        if limit_objects:
            logger.info(f"🔍 Будет проанализировано до {limit_objects} объектов из {total_objects}")
        else:
            logger.info(f"🔍 Будет проанализировано ВСЕ {total_objects} объектов")

        analyzed_objects = []
        processed_count = 0

        try:
            for sheet in sheets:
                sheet_title = sheet.get("title", "Без названия")
                objects = sheet.get("objects", [])

                logger.info(f"--- Анализ листа: {sheet_title} ({len(objects)} объектов) ---")

                for obj in objects:
                    if limit_objects and processed_count >= limit_objects:
                        logger.info(f"⏹️ Достигнут лимит анализа: {limit_objects} объектов")
                        break

                    obj_id = obj.get("name", "")
                    obj_type = obj.get("type", "")

                    if not obj_id:
                        logger.warning("⚠️ Объект без ID, пропускаем")
                        continue

                    # Анализируем объект
                    analysis = self.analyze_object(app_id, obj_id, f"{obj_type}")
                    if "error" not in analysis:
                        analyzed_objects.append(analysis)
                        processed_count += 1
                        if limit_objects:
                            logger.info(f"✅ Объект {processed_count}/{limit_objects} проанализирован")
                        else:
                            logger.info(f"✅ Объект {processed_count}/{total_objects} проанализирован")
                    else:
                        logger.error(f"❌ Ошибка анализа объекта {obj_id}: {analysis}")

                if limit_objects and processed_count >= limit_objects:
                    break

        except Exception as e:
            logger.error(f"💥 Ошибка в процессе анализа: {e}")

        logger.info(f"📊 Анализ завершен: {len(analyzed_objects)} объектов проанализировано")

        return {
            "analyzed_objects": analyzed_objects,
            "total_analyzed": len(analyzed_objects),
            "total_available": total_objects,
            "sheets": sheets
        }

    def test_basic_connection(self) -> bool:
        """Тестирование базового подключения."""
        logger.info("=== ТЕСТ: Базовое подключение ===")

        try:
            # Получаем список документов
            doc_list = self.get_doc_list()
            # logger.info(f"Список документов: {json.dumps(doc_list, indent=2)}")

            return "result" in doc_list and "qDocList" in doc_list["result"]

        except Exception as e:
            logger.error(f"❌ Ошибка в базовом тесте: {e}")
            return False
        finally:
            self.disconnect()

    def test_open_document(self, app_id: str) -> bool:
        """Тестирование открытия документа."""
        logger.info(f"=== ТЕСТ: Открытие документа {app_id} ===")

        try:
            # Открываем документ
            response = self.open_app(app_id)

            if "result" in response and "qReturn" in response["result"]:
                handle = response["result"]["qReturn"]["qHandle"]
                doc_type = response["result"]["qReturn"]["qType"]
                generic_id = response["result"]["qReturn"]["qGenericId"]

                logger.info(f"✅ Документ успешно открыт:")
                logger.info(f"   - Handle: {handle}")
                logger.info(f"   - Type: {doc_type}")
                logger.info(f"   - Generic ID: {generic_id}")

                return True
            else:
                logger.error(f"❌ Ошибка открытия документа: {response}")
                return False

        except Exception as e:
            logger.error(f"💥 Исключение при открытии документа: {e}")
            return False
        finally:
            self.disconnect()

    def test_multiple_documents(self) -> bool:
        """Тестирование открытия нескольких документов через отдельные соединения."""
        logger.info("=== ТЕСТ: Открытие нескольких документов ===")

        # Тестовые ID документов
        test_apps = [
            "e2958865-2aed-4f8a-b3c7-20e6f21d275c",  # dashboard
            "f43e5489-4fd6-4903-83d4-a2d999f983b2",  # dashboard(1)
        ]

        success_count = 0

        for i, app_id in enumerate(test_apps, 1):
            logger.info(f"--- Открытие документа {i}/{len(test_apps)}: {app_id} ---")

            try:
                # Каждый документ открываем через отдельное соединение
                response = self.open_app(app_id)

                if "result" in response and "qReturn" in response["result"]:
                    handle = response["result"]["qReturn"]["qHandle"]
                    doc_type = response["result"]["qReturn"]["qType"]
                    generic_id = response["result"]["qReturn"]["qGenericId"]

                    logger.info(f"✅ Документ {i} успешно открыт:")
                    logger.info(f"   - Handle: {handle}")
                    logger.info(f"   - Type: {doc_type}")
                    logger.info(f"   - Generic ID: {generic_id}")

                    success_count += 1
                else:
                    logger.error(f"❌ Ошибка открытия документа {i}: {response}")

                # Отключаемся от текущего документа перед переходом к следующему
                self.disconnect()

            except Exception as e:
                logger.error(f"💥 Исключение при открытии документа {i}: {e}")
                self.disconnect()

        logger.info(
            f"Результат: {success_count}/{len(test_apps)} документов обработано успешно"
        )
        return success_count == len(test_apps)

    def test_sheets_and_objects(self, app_id: str) -> bool:
        """Тестирование получения листов и объектов приложения."""
        logger.info(f"=== ТЕСТ: Получение листов и объектов для {app_id} ===")

        try:
            # Получаем листы с объектами
            sheets_response = self.get_sheets_with_objects(app_id)

            if "error" in sheets_response:
                logger.error(f"❌ Ошибка получения листов: {sheets_response}")
                return False

            sheets = sheets_response.get("sheets", [])
            total_objects = sheets_response.get("total_objects", 0)

            if not sheets:
                logger.warning("⚠️ В приложении нет листов")
                return True

            logger.info(f"📋 Найдено листов: {len(sheets)}, объектов: {total_objects}")
            return True

        except Exception as e:
            logger.error(f"💥 Исключение в тесте листов и объектов: {e}")
            return False
        finally:
            self.disconnect()

    def test_object_analysis(self, app_id: str) -> bool:
        """Тестирование детального анализа объектов."""
        logger.info(f"=== ТЕСТ: Анализ ВСЕХ объектов для {app_id} ===")

        try:
            # Выполняем полный анализ объектов
            analysis_response = self.analyze_all_objects(app_id)

            if "error" in analysis_response:
                logger.error(f"❌ Ошибка анализа объектов: {analysis_response}")
                return False

            analyzed_objects = analysis_response.get("analyzed_objects", [])
            total_analyzed = analysis_response.get("total_analyzed", 0)
            total_available = analysis_response.get("total_available", 0)

            if not analyzed_objects:
                logger.warning("⚠️ Не удалось проанализировать ни одного объекта")
                return True

            logger.info(f"📊 Результат анализа: {total_analyzed}/{total_available} объектов")

                        # Выводим сводку по типам объектов
            type_counts = {}
            objects_with_measures = 0
            objects_with_dimensions = 0
            objects_with_data = 0

            for obj in analyzed_objects:
                obj_type = obj.get("type", "unknown")
                type_counts[obj_type] = type_counts.get(obj_type, 0) + 1

                if obj.get("measures"):
                    objects_with_measures += 1
                if obj.get("dimensions"):
                    objects_with_dimensions += 1
                if obj.get("data_info"):
                    objects_with_data += 1

            logger.info(f"📈 Типы объектов: {dict(type_counts)}")
            logger.info(f"📏 Объектов с мерами: {objects_with_measures}")
            logger.info(f"📐 Объектов с измерениями: {objects_with_dimensions}")
            logger.info(f"💾 Объектов с данными: {objects_with_data}")

            return True

        except Exception as e:
            logger.error(f"💥 Исключение в тесте анализа объектов: {e}")
            return False
        finally:
            self.disconnect()


def main():
    """Основная функция для тестирования."""
    logger.info("🚀 Запуск тестирования Engine API")
    logger.info(f"Время запуска: {datetime.now()}")

    try:
        # Создаем клиент
        client = QlikEngineTestClient()

        # Тестируем базовое подключение
        if client.test_basic_connection():
            logger.info("✅ Базовое тестирование прошло успешно")
        else:
            logger.error("❌ Базовое тестирование провалилось")
            return

        # Тестируем открытие документов
        if client.test_multiple_documents():
            logger.info("✅ Тестирование открытия документов прошло успешно")
        else:
            logger.error("❌ Тестирование открытия документов провалилось")
            return

        # Тестируем получение листов и объектов
        test_app_id = "e2958865-2aed-4f8a-b3c7-20e6f21d275c"  # dashboard
        if client.test_sheets_and_objects(test_app_id):
            logger.info("✅ Тестирование листов и объектов прошло успешно")
        else:
            logger.error("❌ Тестирование листов и объектов провалилось")
            return

        # Тестируем детальный анализ объектов
        if client.test_object_analysis(test_app_id):
            logger.info("✅ Тестирование анализа объектов прошло успешно")
        else:
            logger.error("❌ Тестирование анализа объектов провалилось")

        # Тест 4: Анализ мастер-элементов
        logger.info(f"=== ТЕСТ: Анализ мастер-элементов для {test_app_id} ===")
        client.analyze_master_items(test_app_id)

        # Тест 5: Анализ переменных
        logger.info(f"=== ТЕСТ: Анализ переменных для {test_app_id} ===")
        client.analyze_variables(test_app_id)

    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        import traceback

        logger.error(f"Трассировка: {traceback.format_exc()}")

    logger.info("🏁 Завершение тестирования")


if __name__ == "__main__":
    main()
