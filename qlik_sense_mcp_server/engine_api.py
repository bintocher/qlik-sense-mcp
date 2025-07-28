"""Qlik Sense Engine API client."""

import json
import websocket
import ssl
from typing import Dict, List, Any, Optional, Union
from datetime import datetime
from .config import QlikSenseConfig


class QlikEngineAPI:
    """Client for Qlik Sense Engine API using WebSocket."""

    def __init__(self, config: QlikSenseConfig):
        self.config = config
        self.ws = None
        self.request_id = 0

    def _get_next_request_id(self) -> int:
        """Get next request ID."""
        self.request_id += 1
        return self.request_id

    def connect(self, app_id: Optional[str] = None) -> None:
        """Connect to Engine API via WebSocket."""
        # Try different WebSocket endpoints
        server_host = self.config.server_url.replace("https://", "").replace(
            "http://", ""
        )

        endpoints_to_try = [
            f"wss://{server_host}:{self.config.engine_port}/app/engineData",
            f"wss://{server_host}:{self.config.engine_port}/app",
            f"ws://{server_host}:{self.config.engine_port}/app/engineData",
            f"ws://{server_host}:{self.config.engine_port}/app",
        ]

        # Setup SSL context
        ssl_context = ssl.create_default_context()
        if not self.config.verify_ssl:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        if self.config.client_cert_path and self.config.client_key_path:
            ssl_context.load_cert_chain(
                self.config.client_cert_path, self.config.client_key_path
            )

        if self.config.ca_cert_path:
            ssl_context.load_verify_locations(self.config.ca_cert_path)

        # Headers for authentication
        headers = [
            f"X-Qlik-User: UserDirectory={self.config.user_directory}; UserId={self.config.user_id}"
        ]

        last_error = None
        for url in endpoints_to_try:
            try:
                if url.startswith("wss://"):
                    self.ws = websocket.create_connection(
                        url, sslopt={"context": ssl_context}, header=headers, timeout=10
                    )
                else:
                    self.ws = websocket.create_connection(
                        url, header=headers, timeout=10
                    )


                self.ws.recv()
                return  # Success
            except Exception as e:
                last_error = e
                if self.ws:
                    self.ws.close()
                    self.ws = None
                continue

        raise ConnectionError(
            f"Failed to connect to Engine API. Last error: {str(last_error)}"
        )

    def disconnect(self) -> None:
        """Disconnect from Engine API."""
        if self.ws:
            self.ws.close()
            self.ws = None

    def send_request(
        self, method: str, params: List[Any] = None, handle: int = -1
    ) -> Dict[str, Any]:
        """
        Send JSON-RPC 2.0 request to Qlik Engine API and return response.

        Args:
            method: Engine API method name
            params: Method parameters list
            handle: Object handle for scoped operations (-1 for global)

        Returns:
            Response dictionary from Engine API
        """
        if not self.ws:
            raise ConnectionError("Not connected to Engine API")


        request = {
            "jsonrpc": "2.0",
            "id": self._get_next_request_id(),
            "handle": handle,
            "method": method,
            "params": params or [],
        }

        self.ws.send(json.dumps(request))


        while True:
            data = self.ws.recv()
            if "result" in data or "error" in data:
                break

        response = json.loads(data)

        if "error" in response:
            raise Exception(f"Engine API error: {response['error']}")

        return response.get("result", {})

    def get_doc_list(self) -> List[Dict[str, Any]]:
        """Get list of available documents."""
        try:
            # Connect to global engine first
            result = self.send_request("GetDocList")
            doc_list = result.get("qDocList", [])

            # Ensure we return a list even if empty
            if isinstance(doc_list, list):
                return doc_list
            else:
                return []

        except Exception as e:
            # Return empty list on error for compatibility
            return []

    def open_doc(self, app_id: str, no_data: bool = True) -> Dict[str, Any]:
        """
        Open Qlik Sense application document.

        Args:
            app_id: Application ID to open
            no_data: If True, open without loading data (faster for metadata operations)

        Returns:
            Response with document handle
        """
        try:
            if no_data:
                return self.send_request("OpenDoc", [app_id, "", "", "", True])
            else:
                return self.send_request("OpenDoc", [app_id])
        except Exception as e:
            # If app is already open, try to get existing handle
            if "already open" in str(e).lower():
                try:
                    # Try to get the already open document
                    doc_list = self.get_doc_list()
                    for doc in doc_list:
                        if doc.get("qDocId") == app_id:
                            # Return mock response with existing handle
                            return {
                                "qReturn": {
                                    "qHandle": doc.get("qHandle", -1),
                                    "qGenericId": app_id
                                }
                            }
                except:
                    pass
            raise e

    def close_doc(self, app_handle: int) -> bool:
        """Close application document."""
        try:
            result = self.send_request("CloseDoc", [], handle=app_handle)
            return result.get("qReturn", {}).get("qSuccess", False)
        except Exception:
            return False

    def get_active_doc(self) -> Dict[str, Any]:
        """Get currently active document if any."""
        try:
            result = self.send_request("GetActiveDoc")
            return result
        except Exception:
            return {}

    def open_doc_safe(self, app_id: str, no_data: bool = True) -> Dict[str, Any]:
        """
        Safely open document with better error handling for already open apps.

        Args:
            app_id: Application ID to open
            no_data: If True, open without loading data

        Returns:
            Response with document handle
        """
        try:
            # First try to open normally
            if no_data:
                return self.send_request("OpenDoc", [app_id, "", "", "", True])
            else:
                return self.send_request("OpenDoc", [app_id])

        except Exception as e:
            error_msg = str(e)

            # Handle "already open" errors specially
            if "already open" in error_msg.lower() or "app already open" in error_msg.lower():
                try:
                    # Try to get active document
                    active_doc = self.get_active_doc()
                    if active_doc and "qReturn" in active_doc:
                        return active_doc

                    # Try to find in document list
                    doc_list = self.get_doc_list()
                    for doc in doc_list:
                        if doc.get("qDocId") == app_id or doc.get("qDocName") == app_id:
                            return {
                                "qReturn": {
                                    "qHandle": doc.get("qHandle", -1),
                                    "qGenericId": app_id
                                }
                            }

                    # If still not found, re-raise original error
                    raise e

                except Exception:
                    # If all recovery attempts fail, re-raise original error
                    raise e
            else:
                # For other errors, just re-raise
                raise e

    def get_app_properties(self, app_handle: int) -> Dict[str, Any]:
        """Get app properties."""
        return self.send_request("GetAppProperties", handle=app_handle)

    def get_script(self, app_handle: int) -> str:
        """Get load script."""
        result = self.send_request("GetScript", [], handle=app_handle)
        return result.get("qScript", "")

    def set_script(self, app_handle: int, script: str) -> bool:
        """Set load script."""
        result = self.send_request("SetScript", [script], handle=app_handle)
        return result.get("qReturn", {}).get("qSuccess", False)

    def do_save(self, app_handle: int, file_name: Optional[str] = None) -> bool:
        """Save app."""
        params = {}
        if file_name:
            params["qFileName"] = file_name
        result = self.send_request("DoSave", params, handle=app_handle)
        return result.get("qReturn", {}).get("qSuccess", False)

    def get_objects(
        self, app_handle: int, object_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get app objects."""
        # Build parameters based on whether specific object_type is requested
        if object_type:
            # Get specific object type
            params = {
                "qOptions": {
                    "qTypes": [object_type],
                    "qIncludeSessionObjects": True,
                    "qData": {},
                }
            }
        else:
            # Get ALL objects - don't specify qTypes to get everything including extensions
            params = {
                "qOptions": {
                    "qIncludeSessionObjects": True,
                    "qData": {},
                }
            }

        # Debug logging
        print(f"DEBUG: get_objects params: {params}")

        result = self.send_request("GetObjects", params, handle=app_handle)

        # Debug result
        if "error" in str(result) or "Missing Types" in str(result):
            print(f"DEBUG: get_objects error result: {result}")

        return result.get("qList", {}).get("qItems", [])

    def get_sheets(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get app sheets."""
        try:
            # Шаг 1: Создаем SheetList объект
            sheet_list_def = {
                "qInfo": {
                    "qType": "SheetList"
                },
                "qAppObjectListDef": {
                    "qType": "sheet",
                    "qData": {
                        "title": "/qMetaDef/title",
                        "description": "/qMetaDef/description",
                        "thumbnail": "/thumbnail",
                        "cells": "/cells",
                        "rank": "/rank",
                        "columns": "/columns",
                        "rows": "/rows"
                    }
                }
            }

            # Создаем session object для списка листов
            create_result = self.send_request("CreateSessionObject", [sheet_list_def], handle=app_handle)

            if "qReturn" not in create_result or "qHandle" not in create_result["qReturn"]:
                print(f"WARNING: Failed to create SheetList object: {create_result}")
                return []

            sheet_list_handle = create_result["qReturn"]["qHandle"]

            # Шаг 2: Получаем layout со списком листов
            layout_result = self.send_request("GetLayout", [], handle=sheet_list_handle)

            if "qLayout" not in layout_result or "qAppObjectList" not in layout_result["qLayout"]:
                print(f"WARNING: No sheet list in layout: {layout_result}")
                return []

            sheets = layout_result["qLayout"]["qAppObjectList"]["qItems"]
            print(f"INFO: Found {len(sheets)} sheets")

            return sheets

        except Exception as e:
            print(f"ERROR: get_sheets exception: {str(e)}")
            return []

    def get_sheet_objects(self, app_handle: int, sheet_id: str) -> List[Dict[str, Any]]:
        """Get objects on a specific sheet."""
        try:
            # First get the sheet object
            sheet_params = {"qId": sheet_id}
            sheet_result = self.send_request(
                "GetObject", sheet_params, handle=app_handle
            )

            if not sheet_result or "qReturn" not in sheet_result:
                return {"error": "Could not get sheet object", "sheet_id": sheet_id}

            sheet_handle = sheet_result["qReturn"]["qHandle"]

            # Get sheet layout to find child objects
            layout_result = self.send_request("GetLayout", {}, handle=sheet_handle)

            if not layout_result or "qLayout" not in layout_result:
                return {"error": "Could not get sheet layout", "sheet_id": sheet_id}

            # Extract child objects from layout
            layout = layout_result["qLayout"]
            child_objects = []

            # Look for cells or children in the layout
            if "qChildList" in layout:
                child_objects = layout["qChildList"]["qItems"]
            elif "cells" in layout:
                child_objects = layout["cells"]
            elif "qChildren" in layout:
                child_objects = layout["qChildren"]

            return child_objects

        except Exception as e:
            return {
                "error": str(e),
                "details": f"Error getting objects for sheet {sheet_id}",
            }

    def get_sheets_with_objects(self, app_id: str) -> Dict[str, Any]:
        """Get sheets and their objects with detailed field usage analysis."""
        try:
            self.connect()

            # Open the app
            app_result = self.open_doc(app_id, no_data=False)
            if "qReturn" not in app_result or "qHandle" not in app_result["qReturn"]:
                return {"error": "Failed to open app", "response": app_result}

            app_handle = app_result["qReturn"]["qHandle"]

            # Get sheets using correct API sequence
            sheets = self.get_sheets(app_handle)
            print(f"DEBUG: get_sheets returned {len(sheets)} sheets")

            if not sheets:
                return {
                    "sheets": [],
                    "total_sheets": 0,
                    "field_usage": {},
                    "debug_info": {
                        "sheets_from_api": 0,
                        "error_reason": "get_sheets returned empty list"
                    }
                }

            # Get detailed info for each sheet and its objects
            detailed_sheets = []
            field_usage_map = {}  # {field_name: {objects: [], sheets: []}}

            for sheet in sheets:
                if not isinstance(sheet, dict) or "qInfo" not in sheet:
                    continue

                sheet_id = sheet["qInfo"]["qId"]
                sheet_title = sheet.get("qMeta", {}).get("title", "")

                print(f"INFO: Processing sheet {sheet_id}: {sheet_title}")

                # Получаем объекты листа правильным способом
                sheet_objects = self._get_sheet_objects_detailed(app_handle, sheet_id)

                # Анализируем поля в объектах
                for obj in sheet_objects:
                    if isinstance(obj, dict) and "fields_used" in obj:
                        for field_name in obj["fields_used"]:
                            if field_name not in field_usage_map:
                                field_usage_map[field_name] = {"objects": [], "sheets": []}

                            # Добавляем объект
                            field_usage_map[field_name]["objects"].append({
                                "object_id": obj.get("object_id", ""),
                                "object_type": obj.get("object_type", ""),
                                "object_title": obj.get("object_title", ""),
                                "sheet_id": sheet_id,
                                "sheet_title": sheet_title
                            })

                            # Добавляем лист (если еще не добавлен)
                            sheet_already_added = any(
                                s["sheet_id"] == sheet_id
                                for s in field_usage_map[field_name]["sheets"]
                            )
                            if not sheet_already_added:
                                field_usage_map[field_name]["sheets"].append({
                                    "sheet_id": sheet_id,
                                    "sheet_title": sheet_title
                                })

                sheet_info = {
                    "sheet_info": sheet,
                    "objects": sheet_objects,
                    "objects_count": len(sheet_objects)
                }
                detailed_sheets.append(sheet_info)

            return {
                "sheets": detailed_sheets,
                "total_sheets": len(detailed_sheets),
                "field_usage": field_usage_map,
                "debug_info": {
                    "sheets_from_api": len(sheets),
                    "processed_sheets": len(detailed_sheets),
                    "fields_with_usage": len([k for k, v in field_usage_map.items() if v["objects"]])
                }
            }

        except Exception as e:
            return {
                "error": str(e),
                "details": "Error in get_sheets_with_objects method",
            }

    def _get_sheet_objects_detailed(self, app_handle: int, sheet_id: str) -> List[Dict[str, Any]]:
        """Get detailed information about objects on a sheet."""
        try:
            # Шаг 1: Получаем handle листа
            sheet_result = self.send_request("GetObject", {"qId": sheet_id}, handle=app_handle)

            if "qReturn" not in sheet_result or "qHandle" not in sheet_result["qReturn"]:
                print(f"WARNING: Failed to get sheet object {sheet_id}: {sheet_result}")
                return []

            sheet_handle = sheet_result["qReturn"]["qHandle"]

            # Шаг 2: Получаем layout листа с объектами
            sheet_layout = self.send_request("GetLayout", [], handle=sheet_handle)

            if "qLayout" not in sheet_layout or "qChildList" not in sheet_layout["qLayout"]:
                print(f"WARNING: No child objects in sheet {sheet_id}")
                return []

            child_objects = sheet_layout["qLayout"]["qChildList"]["qItems"]
            detailed_objects = []

            # Шаг 3: Получаем детальную информацию для каждого объекта
            for child_obj in child_objects:
                obj_id = child_obj.get("qInfo", {}).get("qId", "")
                obj_type = child_obj.get("qInfo", {}).get("qType", "")

                if not obj_id:
                    continue

                try:
                    # Получаем handle объекта
                    obj_result = self.send_request("GetObject", {"qId": obj_id}, handle=app_handle)

                    if "qReturn" not in obj_result or "qHandle" not in obj_result["qReturn"]:
                        continue

                    obj_handle = obj_result["qReturn"]["qHandle"]

                    # Получаем layout объекта
                    obj_layout = self.send_request("GetLayout", [], handle=obj_handle)

                    if "qLayout" not in obj_layout:
                        continue

                    # Анализируем поля в объекте
                    fields_used = self._extract_fields_from_object(obj_layout["qLayout"])

                    detailed_obj = {
                        "object_id": obj_id,
                        "object_type": obj_type,
                        "object_title": obj_layout["qLayout"].get("title", ""),
                        "object_subtitle": obj_layout["qLayout"].get("subtitle", ""),
                        "fields_used": fields_used,
                        "basic_info": child_obj,
                        "detailed_layout": obj_layout["qLayout"]
                    }

                    detailed_objects.append(detailed_obj)
                    print(f"INFO: Processed object {obj_id} ({obj_type}) with {len(fields_used)} fields")

                except Exception as obj_error:
                    print(f"WARNING: Error processing object {obj_id}: {obj_error}")
                    continue

            return detailed_objects

        except Exception as e:
            print(f"ERROR: _get_sheet_objects_detailed error: {str(e)}")
            return []

    def _extract_fields_from_object(self, obj_layout: Dict[str, Any]) -> List[str]:
        """Extract field names used in an object layout."""
        fields = set()

        try:
            # Анализируем HyperCube
            if "qHyperCube" in obj_layout:
                hypercube = obj_layout["qHyperCube"]

                # Dimensions
                for dim_info in hypercube.get("qDimensionInfo", []):
                    field_defs = dim_info.get("qGroupFieldDefs", [])
                    for field_def in field_defs:
                        field_name = self._extract_field_name_from_expression(field_def)
                        if field_name:
                            fields.add(field_name)

                # Measures
                for measure_info in hypercube.get("qMeasureInfo", []):
                    measure_def = measure_info.get("qDef", "")
                    extracted_fields = self._extract_fields_from_expression(measure_def)
                    fields.update(extracted_fields)

            # Анализируем ListObject
            if "qListObject" in obj_layout:
                list_obj = obj_layout["qListObject"]

                for dim_info in list_obj.get("qDimensionInfo", []):
                    field_defs = dim_info.get("qGroupFieldDefs", [])
                    for field_def in field_defs:
                        field_name = self._extract_field_name_from_expression(field_def)
                        if field_name:
                            fields.add(field_name)

            # Анализируем другие типы объектов
            # Для filterpane, kpi и других специальных объектов
            if "qChildList" in obj_layout:
                for child in obj_layout["qChildList"].get("qItems", []):
                    # Рекурсивно анализируем дочерние объекты
                    pass

        except Exception as e:
            print(f"WARNING: Error extracting fields from object: {e}")

        return list(fields)

    def _extract_field_name_from_expression(self, expression: str) -> Optional[str]:
        """Extract field name from a simple field expression."""
        if not expression:
            return None

        expression = expression.strip()

        # Убираем квадратные скобки для простых полей [FieldName]
        if expression.startswith('[') and expression.endswith(']') and expression.count('[') == 1:
            return expression[1:-1]

        # Если это простое имя поля без скобок и функций
        if ' ' not in expression and '(' not in expression and not any(op in expression for op in ['=', '+', '-', '*', '/']):
            return expression

        return None

    def _extract_fields_from_expression(self, expression: str) -> List[str]:
        """Extract field names from a complex expression."""
        import re
        fields = []

        if not expression:
            return fields

        # Ищем поля в квадратных скобках [FieldName]
        bracket_fields = re.findall(r'\[([^\]]+)\]', expression)
        fields.extend(bracket_fields)

        return list(set(fields))  # Убираем дубликаты

    def get_fields(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get app fields using GetTablesAndKeys method."""
        try:
            # Use correct GetTablesAndKeys method as in qsea.py
            result = self.send_request(
                "GetTablesAndKeys",
                [
                    {"qcx": 1000, "qcy": 1000},  # Max dimensions
                    {"qcx": 0, "qcy": 0},  # Min dimensions
                    30,  # Max tables
                    True,  # Include system tables
                    False,  # Include hidden fields
                ],
                handle=app_handle,
            )


            fields_info = []

            if "qtr" in result:
                for table in result["qtr"]:
                    table_name = table.get("qName", "Unknown")

                    if "qFields" in table:
                        for field in table["qFields"]:
                            field_info = {
                                "field_name": field.get("qName", ""),
                                "table_name": table_name,
                                "data_type": field.get("qType", ""),
                                "is_key": field.get("qIsKey", False),
                                "is_system": field.get("qIsSystem", False),
                                "is_hidden": field.get("qIsHidden", False),
                                "is_semantic": field.get("qIsSemantic", False),
                                "distinct_values": field.get(
                                    "qnTotalDistinctValues", 0
                                ),
                                "present_distinct_values": field.get(
                                    "qnPresentDistinctValues", 0
                                ),
                                "rows_count": field.get("qnRows", 0),
                                "subset_ratio": field.get("qSubsetRatio", 0),
                                "key_type": field.get("qKeyType", ""),
                                "tags": field.get("qTags", []),
                            }
                            fields_info.append(field_info)

            return {
                "fields": fields_info,
                "tables_count": len(result.get("qtr", [])),
                "total_fields": len(fields_info),
            }

        except Exception as e:
            return {"error": str(e), "details": "Error in get_fields method"}

    def get_tables(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get app tables."""
        result = self.send_request("GetTablesList", handle=app_handle)
        return result.get("qtr", [])

    def create_session_object(
        self, app_handle: int, obj_def: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create session object."""
        return self.send_request(
            "CreateSessionObject", {"qProp": obj_def}, handle=app_handle
        )

    def get_object(self, app_handle: int, object_id: str) -> Dict[str, Any]:
        """Get object by ID."""
        return self.send_request("GetObject", {"qId": object_id}, handle=app_handle)

    def evaluate_expression(self, app_handle: int, expression: str) -> Any:
        """Evaluate expression."""
        result = self.send_request(
            "Evaluate", {"qExpression": expression}, handle=app_handle
        )
        return result.get("qReturn", {})

    def select_in_field(
        self, app_handle: int, field_name: str, values: List[str], toggle: bool = False
    ) -> bool:
        """Select values in field."""
        params = {"qFieldName": field_name, "qValues": values, "qToggleMode": toggle}
        result = self.send_request("SelectInField", params, handle=app_handle)
        return result.get("qReturn", False)

    def clear_selections(self, app_handle: int, locked_also: bool = False) -> bool:
        """Clear all selections."""
        params = {"qLockedAlso": locked_also}
        result = self.send_request("ClearAll", params, handle=app_handle)
        return result.get("qReturn", False)

    def get_current_selections(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get current selections."""
        result = self.send_request("GetCurrentSelections", handle=app_handle)
        return result.get("qSelections", [])

    def get_data_model(self, app_handle: int) -> Dict[str, Any]:
        """Get complete data model with tables and associations."""
        try:
            # Use GetAllInfos to get basic structure information
            all_infos = self.send_request("GetAllInfos", [], handle=app_handle)

            # Analyze the objects to understand data structure
            sheets = []
            visualizations = []
            measures = []
            dimensions = []

            for info in all_infos.get("qInfos", []):
                obj_type = info.get("qType", "")
                obj_id = info.get("qId", "")

                if obj_type == "sheet":
                    sheets.append({"id": obj_id, "type": obj_type})
                elif obj_type in [
                    "table",
                    "barchart",
                    "linechart",
                    "piechart",
                    "combochart",
                    "kpi",
                    "listbox",
                ]:
                    visualizations.append({"id": obj_id, "type": obj_type})
                elif obj_type == "measure":
                    measures.append({"id": obj_id, "type": obj_type})
                elif obj_type == "dimension":
                    dimensions.append({"id": obj_id, "type": obj_type})

            return {
                "app_structure": {
                    "total_objects": len(all_infos.get("qInfos", [])),
                    "sheets": sheets,
                    "visualizations": visualizations,
                    "measures": measures,
                    "dimensions": dimensions,
                },
                "raw_info": all_infos,
            }
        except Exception as e:
            return {"error": str(e)}

    def get_field_description(self, app_handle: int, field_name: str) -> Dict[str, Any]:
        """Get detailed field information including values."""
        # Use correct structure as in pyqlikengine
        params = [{"qFieldName": field_name, "qStateName": "$"}]
        result = self.send_request("GetField", params, handle=app_handle)
        return result

    def create_hypercube(
        self,
        app_handle: int,
        dimensions: List[str],
        measures: List[str],
        max_rows: int = 1000,
    ) -> Dict[str, Any]:
        """Create hypercube for data extraction with proper structure."""
        try:
            # Create correct hypercube structure
            hypercube_def = {
                "qDimensions": [
                    {
                        "qDef": {
                            "qFieldDefs": [dim],
                            "qSortCriterias": [
                                {
                                    "qSortByState": 0,
                                    "qSortByFrequency": 0,
                                    "qSortByNumeric": 1,
                                    "qSortByAscii": 1,
                                    "qSortByLoadOrder": 0,
                                    "qSortByExpression": 0,
                                    "qExpression": {"qv": ""},
                                }
                            ],
                        },
                        "qNullSuppression": False,
                        "qIncludeElemValue": True,
                    }
                    for dim in dimensions
                ],
                "qMeasures": [
                    {
                        "qDef": {"qDef": measure, "qLabel": f"Measure_{i}"},
                        "qSortBy": {"qSortByNumeric": -1, "qSortByLoadOrder": 0},
                    }
                    for i, measure in enumerate(measures)
                ],
                "qInitialDataFetch": [
                    {
                        "qTop": 0,
                        "qLeft": 0,
                        "qHeight": max_rows,
                        "qWidth": len(dimensions) + len(measures),
                    }
                ],
                "qSuppressZero": False,
                "qSuppressMissing": False,
                "qMode": "S",
                "qInterColumnSortOrder": list(range(len(dimensions) + len(measures))),
            }

            obj_def = {
                "qInfo": {
                    "qId": f"hypercube-{len(dimensions)}d-{len(measures)}m",
                    "qType": "HyperCube",
                },
                "qHyperCubeDef": hypercube_def,
            }

            result = self.send_request(
                "CreateSessionObject", [obj_def], handle=app_handle
            )

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                return {"error": "Failed to create hypercube", "response": result}

            cube_handle = result["qReturn"]["qHandle"]

            # Получаем layout с данными
            layout = self.send_request("GetLayout", [], handle=cube_handle)

            if "qLayout" not in layout or "qHyperCube" not in layout["qLayout"]:
                return {"error": "No hypercube in layout", "layout": layout}

            hypercube = layout["qLayout"]["qHyperCube"]

            return {
                "hypercube_handle": cube_handle,
                "hypercube_data": hypercube,
                "dimensions": dimensions,
                "measures": measures,
                "total_rows": hypercube.get("qSize", {}).get("qcy", 0),
                "total_columns": hypercube.get("qSize", {}).get("qcx", 0),
            }

        except Exception as e:
            return {"error": str(e), "details": "Error in create_hypercube method"}

    def get_hypercube_data(
        self,
        hypercube_handle: int,
        page_top: int = 0,
        page_height: int = 1000,
        page_left: int = 0,
        page_width: int = 50,
    ) -> Dict[str, Any]:
        """Get data from existing hypercube with pagination."""
        try:
            # Use correct GetHyperCubeData method
            params = [
                {
                    "qPath": "/qHyperCubeDef",
                    "qPages": [
                        {
                            "qTop": page_top,
                            "qLeft": page_left,
                            "qHeight": page_height,
                            "qWidth": page_width,
                        }
                    ],
                }
            ]

            result = self.send_request(
                "GetHyperCubeData", params, handle=hypercube_handle
            )
            return result

        except Exception as e:
            return {"error": str(e), "details": "Error in get_hypercube_data method"}

    def get_table_data(
        self, app_handle: int, table_name: str = None, max_rows: int = 1000
    ) -> Dict[str, Any]:
        """Get data from a specific table by creating hypercube with all table fields."""
        try:
            if not table_name:
                # Get list of available tables
                fields_result = self.get_fields(app_handle)
                if "error" in fields_result:
                    return fields_result

                tables = {}
                for field in fields_result.get("fields", []):
                    table = field.get("table_name", "Unknown")
                    if table not in tables:
                        tables[table] = []
                    tables[table].append(field["field_name"])

                return {
                    "message": "Please specify table_name parameter",
                    "available_tables": tables,
                    "note": "Use one of the available table names to get data",
                }

            # Get fields for specified table
            fields_result = self.get_fields(app_handle)
            if "error" in fields_result:
                return fields_result

            table_fields = []
            for field in fields_result.get("fields", []):
                if field.get("table_name") == table_name:
                    table_fields.append(field["field_name"])

            if not table_fields:
                return {"error": f"Table '{table_name}' not found or has no fields"}

            # Limit number of fields to avoid too wide tables
            max_fields = 20
            if len(table_fields) > max_fields:
                table_fields = table_fields[:max_fields]
                truncated = True
            else:
                truncated = False

            # Create hypercube with all table fields as dimensions
            hypercube_def = {
                "qDimensions": [
                    {
                        "qDef": {
                            "qFieldDefs": [field],
                            "qSortCriterias": [
                                {
                                    "qSortByState": 0,
                                    "qSortByFrequency": 0,
                                    "qSortByNumeric": 1,
                                    "qSortByAscii": 1,
                                    "qSortByLoadOrder": 1,
                                    "qSortByExpression": 0,
                                    "qExpression": {"qv": ""},
                                }
                            ],
                        },
                        "qNullSuppression": False,
                        "qIncludeElemValue": True,
                    }
                    for field in table_fields
                ],
                "qMeasures": [],
                "qInitialDataFetch": [
                    {
                        "qTop": 0,
                        "qLeft": 0,
                        "qHeight": max_rows,
                        "qWidth": len(table_fields),
                    }
                ],
                "qSuppressZero": False,
                "qSuppressMissing": False,
                "qMode": "S",
            }

            obj_def = {
                "qInfo": {"qId": f"table-data-{table_name}", "qType": "HyperCube"},
                "qHyperCubeDef": hypercube_def,
            }

            # Создаем session object
            result = self.send_request(
                "CreateSessionObject", [obj_def], handle=app_handle
            )

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                return {
                    "error": "Failed to create hypercube for table data",
                    "response": result,
                }

            cube_handle = result["qReturn"]["qHandle"]

            # Получаем layout с данными
            layout = self.send_request("GetLayout", [], handle=cube_handle)

            if "qLayout" not in layout or "qHyperCube" not in layout["qLayout"]:
                try:
                    self.send_request(
                        "DestroySessionObject",
                        [f"table-data-{table_name}"],
                        handle=app_handle,
                    )
                except:
                    pass
                return {"error": "No hypercube in layout", "layout": layout}

            hypercube = layout["qLayout"]["qHyperCube"]

            # Process data into convenient format
            table_data = []
            headers = table_fields

            for page in hypercube.get("qDataPages", []):
                for row in page.get("qMatrix", []):
                    row_data = {}
                    for i, cell in enumerate(row):
                        if i < len(headers):
                            row_data[headers[i]] = {
                                "text": cell.get("qText", ""),
                                "numeric": (
                                    cell.get("qNum", None)
                                    if cell.get("qNum") != "NaN"
                                    else None
                                ),
                                "is_numeric": cell.get("qIsNumeric", False),
                                "state": cell.get("qState", "O"),
                            }
                    table_data.append(row_data)

            result_data = {
                "table_name": table_name,
                "headers": headers,
                "data": table_data,
                "total_rows": hypercube.get("qSize", {}).get("qcy", 0),
                "returned_rows": len(table_data),
                "total_columns": len(headers),
                "truncated_fields": truncated,
                "dimension_info": hypercube.get("qDimensionInfo", []),
            }

            # Очищаем созданный объект
            try:
                self.send_request(
                    "DestroySessionObject",
                    [f"table-data-{table_name}"],
                    handle=app_handle,
                )
            except Exception as cleanup_error:
                result_data["cleanup_warning"] = str(cleanup_error)

            return result_data

        except Exception as e:
            return {"error": str(e), "details": "Error in get_table_data method"}

    def get_field_values(
        self,
        app_handle: int,
        field_name: str,
        max_values: int = 100,
        include_frequency: bool = True,
    ) -> Dict[str, Any]:
        """Get field values with frequency information using ListObject."""
        try:
            # Use correct structure
            list_def = {
                "qInfo": {"qId": f"field-values-{field_name}", "qType": "ListObject"},
                "qListObjectDef": {
                    "qStateName": "$",
                    "qLibraryId": "",
                    "qDef": {
                        "qFieldDefs": [field_name],
                        "qFieldLabels": [],
                        "qSortCriterias": [
                            {
                                "qSortByState": 0,
                                "qSortByFrequency": 1 if include_frequency else 0,
                                "qSortByNumeric": 1,
                                "qSortByAscii": 1,
                                "qSortByLoadOrder": 0,
                                "qSortByExpression": 0,
                                "qExpression": {"qv": ""},
                            }
                        ],
                    },
                    "qInitialDataFetch": [
                        {"qTop": 0, "qLeft": 0, "qHeight": max_values, "qWidth": 1}
                    ],
                },
            }

            # Create session object - use correct parameter format
            result = self.send_request(
                "CreateSessionObject", [list_def], handle=app_handle
            )

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                return {"error": "Failed to create session object", "response": result}

            list_handle = result["qReturn"]["qHandle"]

            # Получаем layout с данными
            layout = self.send_request("GetLayout", [], handle=list_handle)

            # Correct path to qListObject - it's in qLayout
            if "qLayout" not in layout or "qListObject" not in layout["qLayout"]:
                # Clean up object before returning error
                try:
                    self.send_request(
                        "DestroySessionObject",
                        [f"field-values-{field_name}"],
                        handle=app_handle,
                    )
                except:
                    pass
                return {"error": "No list object in layout", "layout": layout}

            list_object = layout["qLayout"]["qListObject"]
            values_data = []

            # Process data
            for page in list_object.get("qDataPages", []):
                for row in page.get("qMatrix", []):
                    if row and len(row) > 0:
                        cell = row[0]
                        value_info = {
                            "value": cell.get("qText", ""),
                            "state": cell.get(
                                "qState", "O"
                            ),  # O=Optional, S=Selected, A=Alternative, X=Excluded
                            "numeric_value": cell.get("qNum", None),
                            "is_numeric": cell.get("qIsNumeric", False),
                        }

                        # Add frequency if available
                        if "qFrequency" in cell:
                            value_info["frequency"] = cell.get("qFrequency", 0)

                        values_data.append(value_info)

            # Get general field information
            field_info = {
                "field_name": field_name,
                "values": values_data,
                "total_values": list_object.get("qSize", {}).get("qcy", 0),
                "returned_count": len(values_data),
                "dimension_info": list_object.get("qDimensionInfo", {}),
                "debug_info": {
                    "list_handle": list_handle,
                    "data_pages_count": len(list_object.get("qDataPages", [])),
                    "raw_size": list_object.get("qSize", {}),
                },
            }

            # Очищаем созданный объект
            try:
                self.send_request(
                    "DestroySessionObject",
                    [f"field-values-{field_name}"],
                    handle=app_handle,
                )
            except Exception as cleanup_error:
                field_info["cleanup_warning"] = str(cleanup_error)

            return field_info

        except Exception as e:
            return {"error": str(e), "details": "Error in get_field_values method"}

    def get_field_statistics(self, app_handle: int, field_name: str) -> Dict[str, Any]:
        """Get comprehensive statistics for a field."""
        debug_log = []
        debug_log.append(f"get_field_statistics called with app_handle={app_handle}, field_name={field_name}")
        try:
            # Create expressions for statistics
            stats_expressions = [
                f"Count(DISTINCT [{field_name}])",  # Unique values
                f"Count([{field_name}])",  # Total count
                f"Count({{$<[{field_name}]={{'*'}}>}})",  # Non-null count
                f"Min([{field_name}])",  # Minimum value
                f"Max([{field_name}])",  # Maximum value
                f"Avg([{field_name}])",  # Average value
                f"Sum([{field_name}])",  # Sum (if numeric)
                f"Median([{field_name}])",  # Median
                f"Mode([{field_name}])",  # Mode (most frequent)
                f"Stdev([{field_name}])",  # Standard deviation
            ]
            debug_log.append(f"Created {len(stats_expressions)} expressions: {stats_expressions}")

            # Create hypercube for statistics calculation
            hypercube_def = {
                "qDimensions": [],
                "qMeasures": [
                    {"qDef": {"qDef": expr, "qLabel": f"Stat_{i}"}}
                    for i, expr in enumerate(stats_expressions)
                ],
                "qInitialDataFetch": [
                    {
                        "qTop": 0,
                        "qLeft": 0,
                        "qHeight": 1,
                        "qWidth": len(stats_expressions),
                    }
                ],
                "qSuppressZero": False,
                "qSuppressMissing": False,
            }

            obj_def = {
                "qInfo": {"qId": f"field-stats-{field_name}", "qType": "HyperCube"},
                "qHyperCubeDef": hypercube_def,
            }

            # Create session object
            debug_log.append(f"Creating session object with obj_def: {obj_def}")
            result = self.send_request(
                "CreateSessionObject", [obj_def], handle=app_handle
            )
            debug_log.append(f"CreateSessionObject result: {result}")

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                debug_log.append(f"Failed to create session object, returning error")
                return {
                    "error": "Failed to create statistics hypercube",
                    "response": result,
                    "debug_log": debug_log
                }

            cube_handle = result["qReturn"]["qHandle"]

            # Get layout with data
            layout = self.send_request("GetLayout", [], handle=cube_handle)

            if "qLayout" not in layout or "qHyperCube" not in layout["qLayout"]:
                try:
                    self.send_request(
                        "DestroySessionObject",
                        [f"field-stats-{field_name}"],
                        handle=app_handle,
                    )
                except:
                    pass
                return {"error": "No hypercube in statistics layout", "layout": layout, "debug_log": debug_log}

            hypercube = layout["qLayout"]["qHyperCube"]

            # Extract statistics values
            stats_labels = [
                "unique_values",
                "total_count",
                "non_null_count",
                "min_value",
                "max_value",
                "avg_value",
                "sum_value",
                "median_value",
                "mode_value",
                "std_deviation",
            ]

            statistics = {"field_name": field_name}

            for page in hypercube.get("qDataPages", []):
                for row in page.get("qMatrix", []):
                    for i, cell in enumerate(row):
                        if i < len(stats_labels):
                            stat_name = stats_labels[i]
                            statistics[stat_name] = {
                                "text": cell.get("qText", ""),
                                "numeric": (
                                    cell.get("qNum", None)
                                    if cell.get("qNum") != "NaN"
                                    else None
                                ),
                                "is_numeric": cell.get("qIsNumeric", False),
                            }

                                    # Calculate additional derived statistics
            debug_log.append(f"Statistics before calculation: {statistics}")
            if "total_count" in statistics and "non_null_count" in statistics:
                # Handle None values safely
                total_dict = statistics["total_count"]
                non_null_dict = statistics["non_null_count"]
                debug_log.append(f"total_dict: {total_dict}")
                debug_log.append(f"non_null_dict: {non_null_dict}")

                total = total_dict.get("numeric", 0) if total_dict.get("numeric") is not None else 0
                non_null = non_null_dict.get("numeric", 0) if non_null_dict.get("numeric") is not None else 0
                debug_log.append(f"total: {total} (type: {type(total)})")
                debug_log.append(f"non_null: {non_null} (type: {type(non_null)})")

                if total > 0:
                    debug_log.append(f"Calculating percentages...")
                    debug_log.append(f"Calculation: ({total} - {non_null}) / {total} * 100")
                    statistics["null_percentage"] = round(
                        (total - non_null) / total * 100, 2
                    )
                    statistics["completeness_percentage"] = round(
                        non_null / total * 100, 2
                    )
                    debug_log.append(f"Percentages calculated successfully")

            # Cleanup
            try:
                self.send_request(
                    "DestroySessionObject",
                    [f"field-stats-{field_name}"],
                    handle=app_handle,
                )
            except Exception as cleanup_error:
                statistics["cleanup_warning"] = str(cleanup_error)

            statistics["debug_log"] = debug_log
            return statistics

        except Exception as e:
            import traceback
            debug_log.append(f"Exception in get_field_statistics: {e}")
            debug_log.append(f"Traceback: {traceback.format_exc()}")
            return {
                "error": str(e),
                "details": "Error in get_field_statistics method",
                "traceback": traceback.format_exc(),
                "debug_log": debug_log
            }

    def get_object_data(self, app_handle: int, object_id: str) -> Dict[str, Any]:
        """Get data from existing visualization object."""
        obj_result = self.send_request(
            "GetObject", {"qId": object_id}, handle=app_handle
        )
        obj_handle = obj_result.get("qReturn", {}).get("qHandle", -1)

        if obj_handle != -1:
            layout = self.send_request("GetLayout", handle=obj_handle)
            return layout
        return {}

    def export_data_to_csv(
        self, app_handle: int, object_id: str, file_path: str = "/tmp/export.csv"
    ) -> Dict[str, Any]:
        """Export object data to CSV."""
        params = {
            "qObjectId": object_id,
            "qPath": file_path,
            "qExportState": "A",  # All data
        }
        result = self.send_request("ExportData", params, handle=app_handle)
        return result

    def search_objects(
        self, app_handle: int, search_terms: List[str], object_types: List[str] = None
    ) -> List[Dict[str, Any]]:
        """Search for objects by terms."""
        params = {
            "qOptions": {"qSearchFields": ["*"], "qContext": "LockedFieldsOnly"},
            "qTerms": search_terms,
            "qPage": {"qOffset": 0, "qCount": 100, "qMaxNbrFieldMatches": 5},
        }

        if object_types:
            params["qOptions"]["qTypes"] = object_types

        result = self.send_request("SearchObjects", params, handle=app_handle)
        return result.get("qResult", {}).get("qSearchTerms", [])

    def get_field_and_variable_list(self, app_handle: int) -> Dict[str, Any]:
        """Get comprehensive list of fields and variables."""
        result = self.send_request("GetFieldAndVariableList", {}, handle=app_handle)
        return result

    def get_measures(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get master measures."""
        result = self.send_request("GetMeasureList", handle=app_handle)
        return result.get("qMeasureList", {}).get("qItems", [])

    def get_dimensions(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get master dimensions."""
        result = self.send_request("GetDimensionList", handle=app_handle)
        return result.get("qDimensionList", {}).get("qItems", [])

    def get_variables(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get variables."""
        result = self.send_request("GetVariableList", handle=app_handle)
        return result.get("qVariableList", {}).get("qItems", [])

    def create_list_object(
        self, app_handle: int, field_name: str, sort_by_frequency: bool = True
    ) -> Dict[str, Any]:
        """Create optimized list object for field analysis."""
        list_def = {
            "qInfo": {"qType": "ListObject"},
            "qListObjectDef": {
                "qDef": {
                    "qFieldDefs": [field_name],
                    "qSortCriterias": [
                        {
                            "qSortByFrequency": 1 if sort_by_frequency else 0,
                            "qSortByNumeric": 1,
                            "qSortByAscii": 1,
                        }
                    ],
                },
                "qInitialDataFetch": [
                    {"qTop": 0, "qLeft": 0, "qHeight": 100, "qWidth": 1}
                ],
            },
        }

        result = self.send_request(
            "CreateSessionObject", {"qProp": list_def}, handle=app_handle
        )
        return result

    def get_pivot_table_data(
        self,
        app_handle: int,
        dimensions: List[str],
        measures: List[str],
        max_rows: int = 1000,
    ) -> Dict[str, Any]:
        """Create pivot table for complex data analysis."""
        pivot_def = {
            "qInfo": {"qType": "PivotTable"},
            "qHyperCubeDef": {
                "qDimensions": [
                    {"qDef": {"qFieldDefs": [dim]}, "qNullSuppression": True}
                    for dim in dimensions
                ],
                "qMeasures": [
                    {"qDef": {"qDef": measure}, "qSortBy": {"qSortByNumeric": -1}}
                    for measure in measures
                ],
                "qInitialDataFetch": [
                    {
                        "qTop": 0,
                        "qLeft": 0,
                        "qHeight": max_rows,
                        "qWidth": len(dimensions) + len(measures),
                    }
                ],
                "qSuppressZero": True,
                "qSuppressMissing": True,
            },
        }

        result = self.send_request(
            "CreateSessionObject", {"qProp": pivot_def}, handle=app_handle
        )
        return result

    def calculate_expression(
        self, app_handle: int, expression: str, dimensions: List[str] = None
    ) -> Dict[str, Any]:
        """Calculate expression with optional grouping by dimensions."""
        if dimensions:
            # Create hypercube for grouped calculation
            hypercube_def = {
                "qDimensions": [{"qDef": {"qFieldDefs": [dim]}} for dim in dimensions],
                "qMeasures": [{"qDef": {"qDef": expression}}],
                "qInitialDataFetch": [
                    {
                        "qTop": 0,
                        "qLeft": 0,
                        "qHeight": 1000,
                        "qWidth": len(dimensions) + 1,
                    }
                ],
            }

            obj_def = {
                "qInfo": {"qType": "calculation"},
                "qHyperCubeDef": hypercube_def,
            }

            result = self.send_request(
                "CreateSessionObject", {"qProp": obj_def}, handle=app_handle
            )
            return result
        else:
            # Simple expression evaluation
            return self.evaluate_expression(app_handle, expression)

    def get_bookmarks(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get bookmarks (saved selections)."""
        result = self.send_request("GetBookmarkList", handle=app_handle)
        return result.get("qBookmarkList", {}).get("qItems", [])

    def apply_bookmark(self, app_handle: int, bookmark_id: str) -> bool:
        """Apply bookmark selections."""
        result = self.send_request(
            "ApplyBookmark", {"qBookmarkId": bookmark_id}, handle=app_handle
        )
        return result.get("qReturn", False)

    def get_locale_info(self, app_handle: int) -> Dict[str, Any]:
        """Get locale information for proper number/date formatting."""
        result = self.send_request("GetLocaleInfo", handle=app_handle)
        return result

    def search_suggest(
        self, app_handle: int, search_terms: List[str], object_types: List[str] = None
    ) -> List[Dict[str, Any]]:
        """Get search suggestions for better field/value discovery."""
        params = {
            "qSuggestions": {
                "qSuggestionTypes": (
                    ["Field", "Value", "Object"] if not object_types else object_types
                )
            },
            "qTerms": search_terms,
        }

        result = self.send_request("SearchSuggest", params, handle=app_handle)
        return result.get("qResult", {}).get("qSuggestions", [])

    def create_data_export(
        self,
        app_handle: int,
        table_name: str = None,
        fields: List[str] = None,
        format_type: str = "json",
        max_rows: int = 10000,
        filters: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Create data export in various formats (JSON, CSV-like structure)."""
        try:
            # If no specific fields provided, get all fields from table
            if not fields:
                if table_name:
                    fields_result = self.get_fields(app_handle)
                    if "error" in fields_result:
                        return fields_result

                    table_fields = []
                    for field in fields_result.get("fields", []):
                        if field.get("table_name") == table_name:
                            table_fields.append(field["field_name"])

                    if not table_fields:
                        return {"error": f"No fields found for table '{table_name}'"}

                    fields = table_fields[:50]  # Limit to 50 fields max
                else:
                    return {
                        "error": "Either table_name or fields list must be provided"
                    }

            # Create hypercube for data extraction
            hypercube_def = {
                "qDimensions": [
                    {
                        "qDef": {
                            "qFieldDefs": [field],
                            "qSortCriterias": [
                                {
                                    "qSortByState": 0,
                                    "qSortByFrequency": 0,
                                    "qSortByNumeric": 1,
                                    "qSortByAscii": 1,
                                    "qSortByLoadOrder": 1,
                                    "qSortByExpression": 0,
                                    "qExpression": {"qv": ""},
                                }
                            ],
                        },
                        "qNullSuppression": False,
                        "qIncludeElemValue": True,
                    }
                    for field in fields
                ],
                "qMeasures": [],
                "qInitialDataFetch": [
                    {"qTop": 0, "qLeft": 0, "qHeight": max_rows, "qWidth": len(fields)}
                ],
                "qSuppressZero": False,
                "qSuppressMissing": False,
                "qMode": "S",
            }

            # Apply filters if provided
            if filters:
                # Add selection expressions as calculated dimensions
                for field_name, filter_values in filters.items():
                    if isinstance(filter_values, list):
                        values_str = ", ".join([f"'{v}'" for v in filter_values])
                        filter_expr = f"If(Match([{field_name}], {values_str}), [{field_name}], Null())"
                    else:
                        filter_expr = f"If([{field_name}] = '{filter_values}', [{field_name}], Null())"

                    # Replace the original field with filtered version
                    for dim in hypercube_def["qDimensions"]:
                        if dim["qDef"]["qFieldDefs"][0] == field_name:
                            dim["qDef"]["qFieldDefs"] = [filter_expr]
                            break

            obj_def = {
                "qInfo": {
                    "qId": f"data-export-{table_name or 'custom'}",
                    "qType": "HyperCube",
                },
                "qHyperCubeDef": hypercube_def,
            }

            # Create session object
            result = self.send_request(
                "CreateSessionObject", [obj_def], handle=app_handle
            )

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                return {
                    "error": "Failed to create export hypercube",
                    "response": result,
                }

            cube_handle = result["qReturn"]["qHandle"]

            # Get layout with data
            layout = self.send_request("GetLayout", [], handle=cube_handle)

            if "qLayout" not in layout or "qHyperCube" not in layout["qLayout"]:
                try:
                    self.send_request(
                        "DestroySessionObject",
                        [f"data-export-{table_name or 'custom'}"],
                        handle=app_handle,
                    )
                except:
                    pass
                return {"error": "No hypercube in export layout", "layout": layout}

            hypercube = layout["qLayout"]["qHyperCube"]

            # Process data based on format
            export_data = []
            headers = fields

            for page in hypercube.get("qDataPages", []):
                for row in page.get("qMatrix", []):
                    if format_type.lower() == "json":
                        row_data = {}
                        for i, cell in enumerate(row):
                            if i < len(headers):
                                row_data[headers[i]] = {
                                    "text": cell.get("qText", ""),
                                    "numeric": (
                                        cell.get("qNum", None)
                                        if cell.get("qNum") != "NaN"
                                        else None
                                    ),
                                    "is_numeric": cell.get("qIsNumeric", False),
                                }
                        export_data.append(row_data)

                    elif format_type.lower() == "csv":
                        # CSV-like structure (list of values)
                        row_values = []
                        for cell in row:
                            row_values.append(cell.get("qText", ""))
                        export_data.append(row_values)

                    elif format_type.lower() == "simple":
                        # Simple key-value structure
                        row_data = {}
                        for i, cell in enumerate(row):
                            if i < len(headers):
                                row_data[headers[i]] = cell.get("qText", "")
                        export_data.append(row_data)

            result_data = {
                "export_format": format_type,
                "table_name": table_name,
                "fields": headers,
                "data": export_data,
                "metadata": {
                    "total_rows": hypercube.get("qSize", {}).get("qcy", 0),
                    "exported_rows": len(export_data),
                    "total_columns": len(headers),
                    "filters_applied": filters is not None,
                    "export_timestamp": None,  # Could be added with datetime.now() if needed
                    "dimension_info": hypercube.get("qDimensionInfo", []),
                },
            }

            # Add CSV headers if CSV format
            if format_type.lower() == "csv":
                result_data["csv_headers"] = headers

            # Cleanup
            try:
                self.send_request(
                    "DestroySessionObject",
                    [f"data-export-{table_name or 'custom'}"],
                    handle=app_handle,
                )
            except Exception as cleanup_error:
                result_data["cleanup_warning"] = str(cleanup_error)

            return result_data

        except Exception as e:
            return {"error": str(e), "details": "Error in create_data_export method"}

    def get_visualization_data(self, app_handle: int, object_id: str) -> Dict[str, Any]:
        """Get data from existing visualization object (chart, table, etc.)."""
        try:
            # Получаем объект по ID
            obj_result = self.send_request("GetObject", [object_id], handle=app_handle)

            if "qReturn" not in obj_result or "qHandle" not in obj_result["qReturn"]:
                return {
                    "error": f"Failed to get object with ID: {object_id}",
                    "response": obj_result,
                }

            obj_handle = obj_result["qReturn"]["qHandle"]

            # Получаем layout объекта
            layout = self.send_request("GetLayout", [], handle=obj_handle)

            if "qLayout" not in layout:
                return {"error": "No layout found for object", "layout": layout}

            obj_layout = layout["qLayout"]
            obj_info = obj_layout.get("qInfo", {})
            obj_type = obj_info.get("qType", "unknown")

            result = {
                "object_id": object_id,
                "object_type": obj_type,
                "object_title": obj_layout.get("qMeta", {}).get("title", ""),
                "data": None,
                "structure": None,
            }

            # Обрабатываем разные типы объектов
            if "qHyperCube" in obj_layout:
                # Объект с hypercube (большинство графиков и таблиц)
                hypercube = obj_layout["qHyperCube"]

                # Извлекаем данные
                table_data = []
                dimensions = []
                measures = []

                # Получаем информацию о dimensions
                for dim_info in hypercube.get("qDimensionInfo", []):
                    dimensions.append(
                        {
                            "title": dim_info.get("qFallbackTitle", ""),
                            "field": (
                                dim_info.get("qGroupFieldDefs", [""])[0]
                                if dim_info.get("qGroupFieldDefs")
                                else ""
                            ),
                            "cardinal": dim_info.get("qCardinal", 0),
                        }
                    )

                # Получаем информацию о measures
                for measure_info in hypercube.get("qMeasureInfo", []):
                    measures.append(
                        {
                            "title": measure_info.get("qFallbackTitle", ""),
                            "expression": measure_info.get("qDef", ""),
                            "format": measure_info.get("qNumFormat", {}),
                        }
                    )

                # Извлекаем данные из страниц
                for page in hypercube.get("qDataPages", []):
                    for row in page.get("qMatrix", []):
                        row_data = {}

                        # Dimensions
                        for i, cell in enumerate(row[: len(dimensions)]):
                            if i < len(dimensions):
                                row_data[f"dim_{i}_{dimensions[i]['title']}"] = {
                                    "text": cell.get("qText", ""),
                                    "numeric": (
                                        cell.get("qNum", None)
                                        if cell.get("qNum") != "NaN"
                                        else None
                                    ),
                                    "state": cell.get("qState", "O"),
                                }

                        # Measures
                        for i, cell in enumerate(row[len(dimensions) :]):
                            if i < len(measures):
                                row_data[f"measure_{i}_{measures[i]['title']}"] = {
                                    "text": cell.get("qText", ""),
                                    "numeric": (
                                        cell.get("qNum", None)
                                        if cell.get("qNum") != "NaN"
                                        else None
                                    ),
                                }

                        table_data.append(row_data)

                result["data"] = table_data
                result["structure"] = {
                    "dimensions": dimensions,
                    "measures": measures,
                    "total_rows": hypercube.get("qSize", {}).get("qcy", 0),
                    "total_columns": hypercube.get("qSize", {}).get("qcx", 0),
                    "returned_rows": len(table_data),
                }

            elif "qListObject" in obj_layout:
                # ListBox объект
                list_object = obj_layout["qListObject"]

                values_data = []
                for page in list_object.get("qDataPages", []):
                    for row in page.get("qMatrix", []):
                        if row and len(row) > 0:
                            cell = row[0]
                            values_data.append(
                                {
                                    "value": cell.get("qText", ""),
                                    "state": cell.get("qState", "O"),
                                    "frequency": cell.get("qFrequency", 0),
                                }
                            )

                result["data"] = values_data
                result["structure"] = {
                    "field_name": list_object.get("qDimensionInfo", {}).get(
                        "qFallbackTitle", ""
                    ),
                    "total_values": list_object.get("qSize", {}).get("qcy", 0),
                    "returned_values": len(values_data),
                }

            elif "qPivotTable" in obj_layout:
                # Pivot Table объект
                pivot_table = obj_layout["qPivotTable"]
                result["data"] = pivot_table.get("qDataPages", [])
                result["structure"] = {
                    "type": "pivot_table",
                    "size": pivot_table.get("qSize", {}),
                }

            else:
                # Неизвестный тип объекта - возвращаем raw layout
                result["data"] = obj_layout
                result["structure"] = {"type": "unknown", "raw_layout": True}

            return result

        except Exception as e:
            return {
                "error": str(e),
                "details": "Error in get_visualization_data method",
            }

    def get_detailed_app_metadata(self, app_id: str) -> Dict[str, Any]:
        """Get detailed app metadata similar to /api/v1/apps/{app_id}/data/metadata endpoint."""
        try:
            self.connect()

            # Open the app
            app_result = self.open_doc(app_id, no_data=False)
            if "qReturn" not in app_result or "qHandle" not in app_result["qReturn"]:
                return {"error": "Failed to open app", "response": app_result}

            app_handle = app_result["qReturn"]["qHandle"]

            # Get app layout and properties using correct methods
            try:
                layout = self.send_request("GetAppLayout", [], handle=app_handle)
            except:
                layout = {}

            try:
                properties = self.send_request(
                    "GetAppProperties", [], handle=app_handle
                )
            except:
                properties = {}

            # Get fields information
            fields_result = self.get_fields(app_handle)

            # Get tables information using GetTablesAndKeys
            tables_result = self.send_request(
                "GetTablesAndKeys",
                [
                    {"qcx": 1000, "qcy": 1000},  # Max dimensions
                    {"qcx": 0, "qcy": 0},  # Min dimensions
                    30,  # Max tables
                    True,  # Include system tables
                    False,  # Include hidden fields
                ],
                handle=app_handle,
            )

            # Process fields data
            fields_metadata = []
            if "fields" in fields_result:
                for field in fields_result["fields"]:
                    field_meta = {
                        "name": field.get("field_name", ""),
                        "src_tables": [field.get("table_name", "")],
                        "is_system": field.get("is_system", False),
                        "is_hidden": field.get("is_hidden", False),
                        "is_semantic": field.get("is_semantic", False),
                        "distinct_only": False,
                        "cardinal": field.get("distinct_values", 0),
                        "total_count": field.get("rows_count", 0),
                        "is_locked": False,
                        "always_one_selected": False,
                        "is_numeric": "numeric" in field.get("tags", []),
                        "comment": "",
                        "tags": field.get("tags", []),
                        "byte_size": 0,  # Not available via Engine API
                        "hash": "",  # Not available via Engine API
                    }
                    fields_metadata.append(field_meta)

            # Process tables data
            tables_metadata = []
            if "qtr" in tables_result:
                for table in tables_result["qtr"]:
                    table_meta = {
                        "name": table.get("qName", ""),
                        "is_system": table.get("qIsSystem", False),
                        "is_semantic": table.get("qIsSemantic", False),
                        "is_loose": table.get("qIsLoose", False),
                        "no_of_rows": table.get("qNoOfRows", 0),
                        "no_of_fields": len(table.get("qFields", [])),
                        "no_of_key_fields": len(
                            [
                                f
                                for f in table.get("qFields", [])
                                if f.get("qIsKey", False)
                            ]
                        ),
                        "comment": table.get("qComment", ""),
                        "byte_size": 0,  # Not available via Engine API
                    }
                    tables_metadata.append(table_meta)

            # Get reload metadata if available
            reload_meta = {
                "cpu_time_spent_ms": 0,  # Not available via Engine API
                "hardware": {"logical_cores": 0, "total_memory": 0},
                "peak_memory_bytes": 0,
                "fullReloadPeakMemoryBytes": 0,
                "partialReloadPeakMemoryBytes": 0,
            }

            # Calculate static byte size approximation
            static_byte_size = sum(
                table.get("byte_size", 0) for table in tables_metadata
            )

            # Build response similar to the expected format
            metadata = {
                "reload_meta": reload_meta,
                "static_byte_size": static_byte_size,
                "fields": fields_metadata,
                "tables": tables_metadata,
                "has_section_access": False,  # Would need to check script for this
                "tables_profiling_data": [],
                "is_direct_query_mode": False,
                "usage": "ANALYTICS",
                "source": "engine_api",
                "app_layout": layout,
                "app_properties": properties,
            }

            return metadata

        except Exception as e:
            return {"error": str(e), "details": "Error in get_detailed_app_metadata"}
        finally:
            self.disconnect()

    def get_app_details(self, app_id: str) -> Dict[str, Any]:
        """
        Get comprehensive information about application including data model,
        tables with fields and types, usage analysis, and performance metrics.

        This method combines analysis of:
        - App metadata
        - Data model structure
        - Field usage across objects
        - Master items (measures and dimensions)
        - Variables
        - Performance recommendations

        Returns complete JSON report for the application.
        """
        try:
            self.connect()

            # Собираем все данные
            app_metadata = self._analyze_app_metadata(app_id)
            data_model = self._analyze_data_model(app_id, include_samples=False)
            field_usage = self._analyze_field_usage(app_id)
            master_items = self._analyze_master_items(app_id)
            variables = self._analyze_variables(app_id)

            # Формируем структурированный отчет
            report = {
                "app_metadata": self._format_app_metadata(app_metadata),
                "data_model": {
                    "tables": self._format_tables_info(data_model),
                    "total_tables": len(data_model.get("tables", [])),
                    "total_fields": sum(len(table.get("fields", [])) for table in data_model.get("tables", []))
                },
                "data_fields": {
                    "used_fields": [],
                    "usage_statistics": {
                        "total_used": 0,
                        "by_table": {},
                        "by_object_type": {}
                    }
                },
                "master_items": {
                    "measures": self._format_master_measures(master_items),
                    "dimensions": self._format_master_dimensions(master_items),
                    "total_measures": len(master_items.get("measures", [])),
                    "total_dimensions": len(master_items.get("dimensions", []))
                },
                "variables": {
                    "user_variables": [],
                    "system_variables": [],
                    "total_variables": len(variables.get("variables", []))
                },
                "improvement_opportunities": {
                    "unused_fields": [],
                    "potential_savings": {
                        "fields_to_remove": 0,
                        "tables_affected": set()
                    }
                },
                "summary": {
                    "total_fields": field_usage.get("summary", {}).get("total_fields", 0),
                    "used_fields": field_usage.get("summary", {}).get("used_fields", 0),
                    "unused_fields": field_usage.get("summary", {}).get("unused_fields", 0),
                    "usage_percentage": 0,
                    "analysis_timestamp": datetime.now().isoformat()
                }
            }

            # Обрабатываем используемые поля
            by_object_type = {}
            by_table = {}

            for field_key, field_data in field_usage.get("fields", {}).items():
                table_name = field_data["table"]
                field_name = field_data["field"]
                usage = field_data["usage"]

                if usage["is_used"]:
                    # Получаем примеры значений для поля
                    sample_values = self._get_field_sample_values(app_id, field_name, table_name)

                    used_field = {
                        "field_name": field_name,
                        "table_name": table_name,
                        "data_type": field_data["data_type"],
                        "total_rows": field_data["total_rows"],
                        "distinct_values": field_data["distinct_values"],
                        "usage_count": len(usage["objects"]) + len(usage["variables"]) + len(usage["master_measures"]) + len(usage["master_dimensions"]),
                        "usage_details": {
                            "objects": usage["objects"],
                            "variables": usage["variables"],
                            "master_measures": usage["master_measures"],
                            "master_dimensions": usage["master_dimensions"]
                        },
                        "sample_values": sample_values
                    }

                    report["data_fields"]["used_fields"].append(used_field)

                    # Статистика по таблицам
                    if table_name not in by_table:
                        by_table[table_name] = 0
                    by_table[table_name] += 1

                    # Статистика по типам объектов
                    for obj in usage["objects"]:
                        obj_type = obj.get("object_type", "unknown")
                        if obj_type not in by_object_type:
                            by_object_type[obj_type] = 0
                        by_object_type[obj_type] += 1

                else:
                    # Неиспользуемые поля
                    sample_values = self._get_field_sample_values(app_id, field_name, table_name)

                    unused_field = {
                        "field_name": field_name,
                        "table_name": table_name,
                        "data_type": field_data["data_type"],
                        "total_rows": field_data["total_rows"],
                        "distinct_values": field_data["distinct_values"],
                        "sample_values": sample_values,
                        "recommendation": "Рассмотрите возможность удаления из модели данных"
                    }

                    report["improvement_opportunities"]["unused_fields"].append(unused_field)
                    report["improvement_opportunities"]["potential_savings"]["tables_affected"].add(table_name)

            # Обновляем статистику
            report["data_fields"]["usage_statistics"]["total_used"] = len(report["data_fields"]["used_fields"])
            report["data_fields"]["usage_statistics"]["by_table"] = by_table
            report["data_fields"]["usage_statistics"]["by_object_type"] = by_object_type

            report["improvement_opportunities"]["potential_savings"]["fields_to_remove"] = len(report["improvement_opportunities"]["unused_fields"])
            report["improvement_opportunities"]["potential_savings"]["tables_affected"] = list(report["improvement_opportunities"]["potential_savings"]["tables_affected"])

            # Обрабатываем переменные
            for var in variables.get("variables", []):
                var_data = {
                    "name": var.get("qName", ""),
                    "definition": var.get("qDefinition", ""),
                    "text_value": var.get("qText", ""),
                    "numeric_value": var.get("qNum", None),
                    "is_reserved": var.get("qIsReserved", False),
                    "is_script_created": var.get("qIsScriptCreated", False)
                }

                if var_data["is_reserved"]:
                    report["variables"]["system_variables"].append(var_data)
                else:
                    report["variables"]["user_variables"].append(var_data)

            # Вычисляем процент использования
            total_fields = report["summary"]["total_fields"]
            used_fields = report["summary"]["used_fields"]
            if total_fields > 0:
                report["summary"]["usage_percentage"] = round((used_fields / total_fields) * 100, 1)

            return report

        except Exception as e:
            return {"error": str(e), "details": "Error in get_app_details method"}
        finally:
            self.disconnect()

    def _analyze_app_metadata(self, app_id: str) -> Dict[str, Any]:
        """Полный анализ метаданных приложения."""
        try:
            # Открываем приложение
            app_result = self.open_doc(app_id, no_data=False)
            if "qReturn" not in app_result or "qHandle" not in app_result["qReturn"]:
                return {"error": "Failed to open app", "response": app_result}

            app_handle = app_result["qReturn"]["qHandle"]

            # Получаем layout приложения
            layout_response = self.send_request("GetAppLayout", [], handle=app_handle)
            if "result" not in layout_response or "qLayout" not in layout_response["result"]:
                return {"error": f"Failed to get app layout: {layout_response}"}

            layout = layout_response["result"]["qLayout"]

            # Основная информация
            result = {
                "app_id": app_id,
                "title": layout.get("qTitle", ""),
                "filename": layout.get("qFileName", ""),
                "description": layout.get("description", ""),
                "usage": layout.get("qUsage", ""),
                "has_script": layout.get("qHasScript", False),
                "has_data": layout.get("qHasData", False),
                "size": layout.get("qStaticByteSize", 0),
                "created_date": layout.get("createdDate", ""),
                "modified_date": layout.get("modifiedDate", ""),
                "last_reload_time": layout.get("qLastReloadTime", ""),
                "published": layout.get("published", False),
                "publish_time": layout.get("publishTime", ""),
                "stream": layout.get("stream", {}),
                "privileges": layout.get("privileges", []),
                "localization": layout.get("qLocaleInfo", {})
            }

            return result

        except Exception as e:
            return {"error": str(e)}

    def _analyze_data_model(self, app_id: str, include_samples: bool = True, max_samples: int = 5) -> Dict[str, Any]:
        """Полный анализ модели данных приложения."""
        try:
            # Открываем приложение
            app_result = self.open_doc(app_id, no_data=False)
            if "qReturn" not in app_result or "qHandle" not in app_result["qReturn"]:
                return {"error": "Failed to open app"}

            app_handle = app_result["qReturn"]["qHandle"]

            # Получаем структуру таблиц
            tables_result = self.send_request(
                "GetTablesAndKeys",
                [
                    {"qcx": 0, "qcy": 0},
                    {"qcx": 0, "qcy": 0},
                    0,
                    False,
                    False
                ],
                handle=app_handle,
            )

            if "qtr" not in tables_result:
                return {"tables": [], "summary": {"total_tables": 0, "total_fields": 0}}

            tables = tables_result["qtr"]

            result = {
                "tables": [],
                "summary": {
                    "total_tables": len(tables),
                    "total_fields": 0,
                    "total_rows": 0,
                    "field_types": {}
                }
            }

            for table in tables:
                table_name = table.get("qName", "")
                fields = table.get("qFields", [])

                table_info = {
                    "name": table_name,
                    "fields": [],
                    "field_count": len(fields),
                    "total_rows": 0
                }

                # Анализируем поля таблицы
                for field in fields:
                    field_name = field.get("qName", "")
                    is_present = field.get("qPresent", False)
                    has_duplicates = field.get("qHasDuplicates", False)
                    non_nulls = field.get("qnNonNulls", 0)
                    total_rows = field.get("qnRows", 0)
                    distinct_values = field.get("qnTotalDistinctValues", 0)
                    key_type = field.get("qKeyType", "")
                    tags = field.get("qTags", [])

                    # Определяем тип данных из тегов
                    data_type = "unknown"
                    if "$numeric" in tags:
                        if "$integer" in tags:
                            data_type = "integer"
                        else:
                            data_type = "numeric"
                    elif "$text" in tags:
                        data_type = "text"
                    elif "$date" in tags:
                        data_type = "date"
                    elif "$timestamp" in tags:
                        data_type = "timestamp"

                    field_info = {
                        "name": field_name,
                        "data_type": data_type,
                        "is_present": is_present,
                        "has_duplicates": has_duplicates,
                        "non_nulls": non_nulls,
                        "total_rows": total_rows,
                        "distinct_values": distinct_values,
                        "key_type": key_type,
                        "tags": tags
                    }

                    table_info["fields"].append(field_info)

                    # Обновляем общую статистику
                    if total_rows > table_info["total_rows"]:
                        table_info["total_rows"] = total_rows

                    # Подсчитываем типы полей
                    if data_type in result["summary"]["field_types"]:
                        result["summary"]["field_types"][data_type] += 1
                    else:
                        result["summary"]["field_types"][data_type] = 1

                result["tables"].append(table_info)
                result["summary"]["total_fields"] += len(fields)
                result["summary"]["total_rows"] += table_info["total_rows"]

            return result

        except Exception as e:
            return {"error": str(e)}

    def _analyze_field_usage(self, app_id: str) -> Dict[str, Any]:
        """Анализ использования каждого поля модели данных в приложении."""
        try:
            # Получаем модель данных
            data_model = self._analyze_data_model(app_id, include_samples=False)
            if "error" in data_model:
                return data_model

            # Получаем все объекты
            objects_data = self._analyze_all_objects(app_id)
            if "error" in objects_data:
                return objects_data

            # Получаем переменные
            variables_data = self._analyze_variables(app_id)
            if "error" in variables_data:
                return variables_data

            # Получаем мастер-элементы
            master_items = self._analyze_master_items(app_id)
            if "error" in master_items:
                return master_items

            result = {
                "fields": {},
                "summary": {
                    "total_fields": 0,
                    "used_fields": 0,
                    "unused_fields": 0
                }
            }

            # Анализируем каждое поле
            for table in data_model.get("tables", []):
                table_name = table.get("name", "")

                for field in table.get("fields", []):
                    field_name = field.get("name", "")
                    if not field_name:
                        continue

                    field_key = f"{table_name}.{field_name}"
                    result["fields"][field_key] = {
                        "table": table_name,
                        "field": field_name,
                        "data_type": field.get("data_type", "unknown"),
                        "total_rows": field.get("total_rows", 0),
                        "distinct_values": field.get("distinct_values", 0),
                        "usage": {
                            "objects": [],
                            "variables": [],
                            "master_measures": [],
                            "master_dimensions": [],
                            "is_used": False
                        }
                    }

                    field_usage = result["fields"][field_key]["usage"]

                    # Проверяем использование в объектах
                    for obj_data in objects_data.get("analyzed_objects", []):
                        obj_id = obj_data.get("object_id", "")
                        obj_name = obj_data.get("title", "")
                        obj_type = obj_data.get("type", "")

                        measures = obj_data.get("measures", [])
                        dimensions = obj_data.get("dimensions", [])

                        # Проверяем в мерах
                        for measure in measures:
                            measure_formula = measure.get("qDef", {}).get("qDef", "")
                            measure_name = measure.get("qDef", {}).get("qLabel", "")

                            if self._field_in_expression(field_name, measure_formula, measure_name):
                                field_usage["objects"].append({
                                    "object_id": obj_id,
                                    "object_name": obj_name,
                                    "object_type": obj_type,
                                    "usage_type": "measure",
                                    "formula": measure_formula or measure_name
                                })
                                field_usage["is_used"] = True

                        # Проверяем в измерениях
                        for dimension in dimensions:
                            dimension_formula = dimension.get("qDef", {}).get("qFieldDefs", [""])[0] if dimension.get("qDef", {}).get("qFieldDefs") else ""
                            dimension_name = dimension.get("qDef", {}).get("qLabel", "")

                            if self._field_in_expression(field_name, dimension_formula, dimension_name):
                                field_usage["objects"].append({
                                    "object_id": obj_id,
                                    "object_name": obj_name,
                                    "object_type": obj_type,
                                    "usage_type": "dimension",
                                    "formula": dimension_formula or dimension_name
                                })
                                field_usage["is_used"] = True

                    # Проверяем использование в переменных
                    for variable in variables_data.get("variables", []):
                        var_name = variable.get("qName", "")
                        var_definition = variable.get("qDefinition", "")

                        if self._field_in_expression(field_name, var_definition):
                            field_usage["variables"].append({
                                "variable_name": var_name,
                                "definition": var_definition
                            })
                            field_usage["is_used"] = True

                    # Проверяем использование в мастер-мерах
                    for measure in master_items.get("measures", []):
                        measure_name = measure.get("qMeta", {}).get("title", "")
                        measure_def = measure.get("qMeasure", {}).get("qDef", "")

                        if self._field_in_expression(field_name, measure_def):
                            field_usage["master_measures"].append({
                                "measure_name": measure_name,
                                "definition": measure_def
                            })
                            field_usage["is_used"] = True

                    # Проверяем использование в мастер-измерениях
                    for dimension in master_items.get("dimensions", []):
                        dimension_name = dimension.get("qMeta", {}).get("title", "")
                        field_defs = dimension.get("qDim", {}).get("qFieldDefs", [])

                        for field_def in field_defs:
                            if self._field_in_expression(field_name, field_def):
                                field_usage["master_dimensions"].append({
                                    "dimension_name": dimension_name,
                                    "field_definition": field_def
                                })
                                field_usage["is_used"] = True

                    # Обновляем счетчики
                    result["summary"]["total_fields"] += 1
                    if field_usage["is_used"]:
                        result["summary"]["used_fields"] += 1
                    else:
                        result["summary"]["unused_fields"] += 1

            return result

        except Exception as e:
            return {"error": str(e)}

    def _field_in_expression(self, field_name: str, expression: str, name: str = "") -> bool:
        """Проверяет, используется ли поле в выражении."""
        if not expression and not name:
            return False

        for text in [expression, name]:
            if text and (
                field_name in text or
                f"[{field_name}]" in text or
                f" {field_name}" in text or
                f"({field_name}" in text or
                f"({field_name})" in text or
                text == field_name or
                text == f"[{field_name}]"
            ):
                return True
        return False

    def _analyze_master_items(self, app_id: str) -> Dict[str, Any]:
        """Полный анализ мастер-мер и мастер-измерений приложения."""
        try:
            # Открываем приложение
            app_result = self.open_doc(app_id, no_data=False)
            if "qReturn" not in app_result or "qHandle" not in app_result["qReturn"]:
                return {"error": "Failed to open app"}

            app_handle = app_result["qReturn"]["qHandle"]

            result = {
                "measures": [],
                "dimensions": [],
                "summary": {}
            }

            # Получаем мастер-меры
            measures_result = self._get_master_measures(app_handle)
            result["measures"] = measures_result

            # Получаем мастер-измерения
            dimensions_result = self._get_master_dimensions(app_handle)
            result["dimensions"] = dimensions_result

            # Сводка
            result["summary"] = {
                "total_measures": len(result["measures"]),
                "total_dimensions": len(result["dimensions"]),
                "published_measures": sum(1 for m in result["measures"] if m.get("qMeta", {}).get("published", False)),
                "published_dimensions": sum(1 for d in result["dimensions"] if d.get("qMeta", {}).get("published", False))
            }

            return result

        except Exception as e:
            return {"error": str(e)}

    def _get_master_measures(self, app_handle: int) -> List[Dict[str, Any]]:
        """Получение всех мастер-мер приложения."""
        try:
            # Создаем объект MeasureList
            measure_list_def = {
                "qInfo": {"qType": "MeasureList"},
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

            measure_list_response = self.send_request("CreateSessionObject", [measure_list_def], handle=app_handle)
            if "qReturn" not in measure_list_response or "qHandle" not in measure_list_response["qReturn"]:
                return []

            measure_list_handle = measure_list_response["qReturn"]["qHandle"]

            # Получаем layout с данными
            layout_response = self.send_request("GetLayout", [], handle=measure_list_handle)
            if "qLayout" not in layout_response:
                return []

            layout = layout_response["qLayout"]
            measure_list = layout.get("qMeasureList", {})
            measures = measure_list.get("qItems", [])

            return measures

        except Exception:
            return []

    def _get_master_dimensions(self, app_handle: int) -> List[Dict[str, Any]]:
        """Получение всех мастер-измерений приложения."""
        try:
            # Создаем объект DimensionList
            dimension_list_def = {
                "qInfo": {"qType": "DimensionList"},
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

            dimension_list_response = self.send_request("CreateSessionObject", [dimension_list_def], handle=app_handle)
            if "qReturn" not in dimension_list_response or "qHandle" not in dimension_list_response["qReturn"]:
                return []

            dimension_list_handle = dimension_list_response["qReturn"]["qHandle"]

            # Получаем layout с данными
            layout_response = self.send_request("GetLayout", [], handle=dimension_list_handle)
            if "qLayout" not in layout_response:
                return []

            layout = layout_response["qLayout"]
            dimension_list = layout.get("qDimensionList", {})
            dimensions = dimension_list.get("qItems", [])

            return dimensions

        except Exception:
            return []

    def _analyze_variables(self, app_id: str) -> Dict[str, Any]:
        """Полный анализ всех переменных приложения."""
        try:
            # Открываем приложение
            app_result = self.open_doc(app_id, no_data=False)
            if "qReturn" not in app_result or "qHandle" not in app_result["qReturn"]:
                return {"error": "Failed to open app"}

            app_handle = app_result["qReturn"]["qHandle"]

            # Создаем объект VariableList
            variable_list_def = {
                "qInfo": {"qType": "VariableList"},
                "qVariableListDef": {
                    "qType": "variable",
                    "qShowReserved": True,
                    "qShowConfig": True,
                    "qData": {"tags": "/tags"}
                }
            }

            variable_list_response = self.send_request("CreateSessionObject", [variable_list_def], handle=app_handle)
            if "qReturn" not in variable_list_response or "qHandle" not in variable_list_response["qReturn"]:
                return {"variables": [], "summary": {}}

            variable_list_handle = variable_list_response["qReturn"]["qHandle"]

            # Получаем layout с данными
            layout_response = self.send_request("GetLayout", [], handle=variable_list_handle)
            if "qLayout" not in layout_response:
                return {"variables": [], "summary": {}}

            layout = layout_response["qLayout"]
            variable_list = layout.get("qVariableList", {})
            variables = variable_list.get("qItems", [])

            result = {
                "variables": variables,
                "user_variables": [],
                "system_variables": [],
                "script_variables": [],
                "summary": {}
            }

            for variable in variables:
                is_reserved = variable.get("qIsReserved", False)
                is_script_created = variable.get("qIsScriptCreated", False)

                if is_reserved:
                    result["system_variables"].append(variable)
                else:
                    result["user_variables"].append(variable)

                if is_script_created:
                    result["script_variables"].append(variable)

            # Сводка
            result["summary"] = {
                "total_variables": len(variables),
                "user_variables": len(result["user_variables"]),
                "system_variables": len(result["system_variables"]),
                "script_variables": len(result["script_variables"])
            }

            return result

        except Exception as e:
            return {"error": str(e)}

    def _analyze_all_objects(self, app_id: str) -> Dict[str, Any]:
        """Анализ всех объектов приложения с детальной информацией."""
        try:
            # Открываем приложение
            app_result = self.open_doc(app_id, no_data=False)
            if "qReturn" not in app_result or "qHandle" not in app_result["qReturn"]:
                return {"error": "Failed to open app"}

            app_handle = app_result["qReturn"]["qHandle"]

            # Получаем листы
            sheets = self.get_sheets(app_handle)
            analyzed_objects = []

            for sheet in sheets:
                sheet_id = sheet.get("qInfo", {}).get("qId", "")
                if not sheet_id:
                    continue

                # Получаем объекты листа
                sheet_objects = self.get_sheet_objects(app_handle, sheet_id)

                for obj in sheet_objects:
                    obj_id = obj.get("qInfo", {}).get("qId", "")
                    if not obj_id:
                        continue

                    # Анализируем объект
                    analysis = self._analyze_object(app_handle, obj_id)
                    if "error" not in analysis:
                        analyzed_objects.append(analysis)

            return {
                "analyzed_objects": analyzed_objects,
                "total_analyzed": len(analyzed_objects)
            }

        except Exception as e:
            return {"error": str(e)}

    def _analyze_object(self, app_handle: int, object_id: str) -> Dict[str, Any]:
        """Комплексный анализ объекта: получение handle, layout и properties."""
        try:
            # Получаем объект
            object_response = self.send_request("GetObject", {"qId": object_id}, handle=app_handle)
            if "qReturn" not in object_response or "qHandle" not in object_response["qReturn"]:
                return {"error": "Failed to get object"}

            object_handle = object_response["qReturn"]["qHandle"]
            object_type = object_response["qReturn"]["qGenericType"]

            # Получаем layout
            layout_response = self.send_request("GetLayout", [], handle=object_handle)
            if "qLayout" not in layout_response:
                return {"error": "Failed to get layout"}

            # Получаем properties
            properties_response = self.send_request("GetProperties", [], handle=object_handle)
            if "qProp" not in properties_response:
                return {"error": "Failed to get properties"}

            # Анализируем данные
            layout = layout_response["qLayout"]
            properties = properties_response["qProp"]

            # Извлекаем основную информацию
            title = properties.get("qMetaDef", {}).get("title", "Без названия")
            description = properties.get("qMetaDef", {}).get("description", "")

            # Анализируем меры
            measures = self._extract_measures(properties)

            # Анализируем измерения
            dimensions = self._extract_dimensions(properties)

            return {
                "object_id": object_id,
                "handle": object_handle,
                "type": object_type,
                "title": title,
                "description": description,
                "measures": measures,
                "dimensions": dimensions,
                "layout": layout,
                "properties": properties
            }

        except Exception as e:
            return {"error": str(e)}

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

    def _format_app_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Форматирование метаданных приложения."""
        if "error" in metadata:
            return {"error": metadata["error"]}

        return {
            "app_id": metadata.get("app_id", ""),
            "name": metadata.get("title", ""),
            "description": metadata.get("description", ""),
            "filename": metadata.get("filename", ""),
            "usage_type": metadata.get("usage", ""),
            "size_bytes": metadata.get("size", 0),
            "size_mb": round(metadata.get("size", 0) / (1024 * 1024), 2),
            "has_script": metadata.get("has_script", False),
            "has_data": metadata.get("has_data", False),
            "created_date": metadata.get("created_date", ""),
            "modified_date": metadata.get("modified_date", ""),
            "last_reload_time": metadata.get("last_reload_time", ""),
            "is_published": metadata.get("published", False),
            "publish_time": metadata.get("publish_time", ""),
            "stream": metadata.get("stream", {}),
            "privileges": metadata.get("privileges", []),
            "localization": metadata.get("localization", {})
        }

    def _format_tables_info(self, data_model: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Форматирование информации о таблицах."""
        if "error" in data_model:
            return []

        tables_info = []
        for table in data_model.get("tables", []):
            table_info = {
                "name": table.get("name", ""),
                "total_fields": len(table.get("fields", [])),
                "key_fields": [f["name"] for f in table.get("fields", []) if f.get("key_type") == "PRIMARY_KEY"],
                "field_types": {},
                "total_rows": table.get("total_rows", 0)
            }

            # Группируем поля по типам
            for field in table.get("fields", []):
                field_type = field.get("data_type", "unknown")
                if field_type not in table_info["field_types"]:
                    table_info["field_types"][field_type] = 0
                table_info["field_types"][field_type] += 1

            tables_info.append(table_info)

        return tables_info

    def _format_master_measures(self, master_items: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Форматирование мастер-мер."""
        if "error" in master_items:
            return []

        measures = []
        for measure in master_items.get("measures", []):
            measure_info = {
                "name": measure.get("qMeta", {}).get("title", ""),
                "description": measure.get("qMeta", {}).get("description", ""),
                "definition": measure.get("qMeasure", {}).get("qDef", ""),
                "created_date": measure.get("qMeta", {}).get("createdDate", ""),
                "modified_date": measure.get("qMeta", {}).get("modifiedDate", ""),
                "is_published": measure.get("qMeta", {}).get("published", False),
                "owner": measure.get("qMeta", {}).get("owner", {}).get("name", "")
            }
            measures.append(measure_info)

        return measures

    def _format_master_dimensions(self, master_items: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Форматирование мастер-измерений."""
        if "error" in master_items:
            return []

        dimensions = []
        for dimension in master_items.get("dimensions", []):
            dimension_info = {
                "name": dimension.get("qMeta", {}).get("title", ""),
                "description": dimension.get("qMeta", {}).get("description", ""),
                "field_definitions": dimension.get("qDim", {}).get("qFieldDefs", []),
                "created_date": dimension.get("qMeta", {}).get("createdDate", ""),
                "modified_date": dimension.get("qMeta", {}).get("modifiedDate", ""),
                "is_published": dimension.get("qMeta", {}).get("published", False),
                "owner": dimension.get("qMeta", {}).get("owner", {}).get("name", "")
            }
            dimensions.append(dimension_info)

        return dimensions

    def _get_field_sample_values(self, app_id: str, field_name: str, table_name: str, max_samples: int = 10) -> List[str]:
        """Получение примеров значений для поля."""
        try:
            # Простое получение нескольких значений поля
            return [f"sample_{i}" for i in range(min(3, max_samples))]  # Заглушка
        except Exception:
            return []
