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
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('engine_test.log')
    ]
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

        # Логируем конфигурацию (без секретных данных)
        logger.info(f"Server URL: {self.server_url}")
        logger.info(f"User Directory: {self.user_directory}")
        logger.info(f"User ID: {self.user_id}")
        logger.info(f"Engine Port: {self.engine_port}")
        logger.info(f"Verify SSL: {self.verify_ssl}")
        logger.info(f"Client Cert Path: {self.client_cert_path}")
        logger.info(f"CA Cert Path: {self.ca_cert_path}")

        # Проверяем что все необходимые параметры заданы
        if not all([self.server_url, self.user_directory, self.user_id]):
            raise ValueError("Отсутствуют обязательные переменные окружения: QLIK_SERVER_URL, QLIK_USER_DIRECTORY, QLIK_USER_ID")

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
        logger.info(f"=== Подключение к Engine API {'для приложения ' + app_id if app_id else 'общее'} ===")

        server_host = self.server_url.replace("https://", "").replace("http://", "")
        logger.info(f"Server host: {server_host}")

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
            logger.warning("SSL верификация отключена")
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        if self.client_cert_path and self.client_key_path:
            logger.info(f"Загружаем клиентские сертификаты: {self.client_cert_path}")
            try:
                ssl_context.load_cert_chain(self.client_cert_path, self.client_key_path)
                logger.info("Клиентские сертификаты загружены успешно")
            except Exception as e:
                logger.error(f"Ошибка загрузки клиентских сертификатов: {e}")
                return False

        if self.ca_cert_path:
            logger.info(f"Загружаем CA сертификат: {self.ca_cert_path}")
            try:
                ssl_context.load_verify_locations(self.ca_cert_path)
                logger.info("CA сертификат загружен успешно")
            except Exception as e:
                logger.error(f"Ошибка загрузки CA сертификата: {e}")
                return False

        # Заголовки для аутентификации
        headers = [
            f"X-Qlik-User: UserDirectory={self.user_directory}; UserId={self.user_id}"
        ]
        logger.info(f"Заголовки аутентификации: {headers}")

        # Пробуем подключиться к каждому эндпоинту
        last_error = None
        for i, url in enumerate(endpoints_to_try, 1):
            logger.info(f"Попытка {i}/{len(endpoints_to_try)}: {url}")
            try:
                if url.startswith("wss://"):
                    logger.debug("Создаем WSS соединение")
                    self.ws = websocket.create_connection(
                        url,
                        sslopt={"context": ssl_context},
                        header=headers,
                        timeout=10
                    )
                else:
                    logger.debug("Создаем WS соединение")
                    self.ws = websocket.create_connection(
                        url,
                        header=headers,
                        timeout=10
                    )

                # Получаем первое сообщение от сервера
                logger.debug("Получаем первое сообщение от сервера")
                initial_message = self.ws.recv()
                logger.info(f"Первое сообщение от сервера: {initial_message}")

                logger.info(f"✅ Успешно подключились к: {url}")
                return True

            except Exception as e:
                last_error = e
                logger.warning(f"❌ Ошибка подключения к {url}: {e}")
                if self.ws:
                    self.ws.close()
                    self.ws = None
                continue

        logger.error(f"Не удалось подключиться ни к одному эндпоинту. Последняя ошибка: {last_error}")
        return False

    def disconnect(self) -> None:
        """Отключение от Engine API."""
        logger.info("=== Отключение от Engine API ===")
        if self.ws:
            try:
                self.ws.close()
                logger.info("WebSocket соединение закрыто")
            except Exception as e:
                logger.warning(f"Ошибка при закрытии WebSocket: {e}")
            self.ws = None
        self.app_handle = -1
        self.current_app_id = None

    def send_request(self, method: str, params: List[Any] = None, handle: int = -1) -> Dict[str, Any]:
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
            "params": params
        }

        logger.debug(f">>> Отправляем запрос: {json.dumps(request, indent=2)}")

        try:
            self.ws.send(json.dumps(request))
            response_text = self.ws.recv()
            logger.debug(f"<<< Получен ответ: {response_text}")

            response = json.loads(response_text)

            if "error" in response:
                logger.error(f"Ошибка в ответе: {response['error']}")

            return response

        except Exception as e:
            logger.error(f"Ошибка при отправке запроса: {e}")
            return {"error": str(e)}

    def open_app(self, app_id: str) -> Dict[str, Any]:
        """Открытие приложения (в контексте соединения с конкретным app_id)."""
        logger.info(f"=== Открытие приложения {app_id} ===")

        # Если уже подключены к другому приложению - отключаемся
        if self.current_app_id and self.current_app_id != app_id:
            logger.info(f"Отключаемся от текущего приложения {self.current_app_id}")
            self.disconnect()

        # Подключаемся к конкретному приложению
        if not self.ws or self.current_app_id != app_id:
            if not self.connect(app_id):
                return {"error": "Failed to connect to app"}

        # Открываем документ в контексте этого приложения
        response = self.send_request("OpenDoc", [app_id], handle=-1)

        if "result" in response and "qReturn" in response["result"]:
            self.app_handle = response["result"]["qReturn"]["qHandle"]
            logger.info(f"✅ Приложение открыто, handle: {self.app_handle}")
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

    def test_basic_connection(self) -> bool:
        """Тестирование базового подключения."""
        logger.info("=== ТЕСТ: Базовое подключение ===")

        try:
            # Получаем список документов
            doc_list = self.get_doc_list()
            logger.info(f"Список документов: {json.dumps(doc_list, indent=2)}")

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
            "f43e5489-4fd6-4903-83d4-a2d999f983b2"   # dashboard(1)
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

        logger.info(f"Результат: {success_count}/{len(test_apps)} документов обработано успешно")
        return success_count == len(test_apps)


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

    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        import traceback
        logger.error(f"Трассировка: {traceback.format_exc()}")

    logger.info("🏁 Завершение тестирования")


if __name__ == "__main__":
    main()
