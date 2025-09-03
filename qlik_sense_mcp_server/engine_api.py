"""Qlik Sense Engine API client."""

import json
import websocket
import ssl
from typing import Dict, List, Any, Optional, Union
from datetime import datetime
from .config import QlikSenseConfig
import logging

logger = logging.getLogger(__name__)


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
        logger.debug(f"get_objects params: {params}")

        result = self.send_request("GetObjects", params, handle=app_handle)

        # Debug result
        if "error" in str(result) or "Missing Types" in str(result):
            logger.debug(f"get_objects error result: {result}")

        return result.get("qList", {}).get("qItems", [])

    def get_sheets(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get app sheets."""
        try:
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
                        "rows": "/rows"
                    }
                }
            }

            create_result = self.send_request("CreateSessionObject", [sheet_list_def], handle=app_handle)

            if "qReturn" not in create_result or "qHandle" not in create_result["qReturn"]:
                logger.warning(f"Failed to create SheetList object: {create_result}")
                return []

            sheet_list_handle = create_result["qReturn"]["qHandle"]
            layout_result = self.send_request("GetLayout", [], handle=sheet_list_handle)
            if "qLayout" not in layout_result or "qAppObjectList" not in layout_result["qLayout"]:
                logger.warning(f"No sheet list in layout: {layout_result}")
                return []

            sheets = layout_result["qLayout"]["qAppObjectList"]["qItems"]
            logger.info(f"Found {len(sheets)} sheets")
            return sheets

        except Exception as e:
            logger.error(f"get_sheets exception: {str(e)}")
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
            logger.debug(f"get_sheets returned {len(sheets)} sheets")

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

            detailed_sheets = []
            field_usage_map = {}

            for sheet in sheets:
                if not isinstance(sheet, dict) or "qInfo" not in sheet:
                    continue

                sheet_id = sheet["qInfo"]["qId"]
                sheet_title = sheet.get("qMeta", {}).get("title", "")

                logger.info(f"Processing sheet {sheet_id}: {sheet_title}")

                sheet_objects = self._get_sheet_objects_detailed(app_handle, sheet_id)

                for obj in sheet_objects:
                    if isinstance(obj, dict) and "fields_used" in obj:
                        for field_name in obj["fields_used"]:
                            if field_name not in field_usage_map:
                                field_usage_map[field_name] = {"objects": [], "sheets": []}

                            field_usage_map[field_name]["objects"].append({
                                "object_id": obj.get("object_id", ""),
                                "object_type": obj.get("object_type", ""),
                                "object_title": obj.get("object_title", ""),
                                "sheet_id": sheet_id,
                                "sheet_title": sheet_title
                            })

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
            sheet_result = self.send_request("GetObject", {"qId": sheet_id}, handle=app_handle)
            if "qReturn" not in sheet_result or "qHandle" not in sheet_result["qReturn"]:
                logger.warning(f"Failed to get sheet object {sheet_id}: {sheet_result}")
                return []

            sheet_handle = sheet_result["qReturn"]["qHandle"]
            sheet_layout = self.send_request("GetLayout", [], handle=sheet_handle)
            if "qLayout" not in sheet_layout or "qChildList" not in sheet_layout["qLayout"]:
                logger.warning(f"No child objects in sheet {sheet_id}")
                return []

            child_objects = sheet_layout["qLayout"]["qChildList"]["qItems"]
            detailed_objects = []

            for child_obj in child_objects:
                obj_id = child_obj.get("qInfo", {}).get("qId", "")
                obj_type = child_obj.get("qInfo", {}).get("qType", "")
                if not obj_id:
                    continue
                try:
                    obj_result = self.send_request("GetObject", {"qId": obj_id}, handle=app_handle)
                    if "qReturn" not in obj_result or "qHandle" not in obj_result["qReturn"]:
                        continue
                    obj_handle = obj_result["qReturn"]["qHandle"]
                    obj_layout = self.send_request("GetLayout", [], handle=obj_handle)
                    if "qLayout" not in obj_layout:
                        continue
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
                    logger.info(f"Processed object {obj_id} ({obj_type}) with {len(fields_used)} fields")
                except Exception as obj_error:
                    logger.warning(f"Error processing object {obj_id}: {obj_error}")
                    continue

            return detailed_objects

        except Exception as e:
            logger.error(f"_get_sheet_objects_detailed error: {str(e)}")
            return []

    def _extract_fields_from_object(self, obj_layout: Dict[str, Any]) -> List[str]:
        """Extract field names used in an object layout."""
        fields = set()
        try:
            if "qHyperCube" in obj_layout:
                hypercube = obj_layout["qHyperCube"]
                for dim_info in hypercube.get("qDimensionInfo", []):
                    field_defs = dim_info.get("qGroupFieldDefs", [])
                    for field_def in field_defs:
                        field_name = self._extract_field_name_from_expression(field_def)
                        if field_name:
                            fields.add(field_name)
                for measure_info in hypercube.get("qMeasureInfo", []):
                    measure_def = measure_info.get("qDef", "")
                    extracted_fields = self._extract_fields_from_expression(measure_def)
                    fields.update(extracted_fields)

            if "qListObject" in obj_layout:
                list_obj = obj_layout["qListObject"]
                for dim_info in list_obj.get("qDimensionInfo", []):
                    field_defs = dim_info.get("qGroupFieldDefs", [])
                    for field_def in field_defs:
                        field_name = self._extract_field_name_from_expression(field_def)
                        if field_name:
                            fields.add(field_name)

            if "qChildList" in obj_layout:
                for child in obj_layout["qChildList"].get("qItems", []):
                    pass

        except Exception as e:
            logger.warning(f"Error extracting fields from object: {e}")

        return list(fields)

    def _extract_field_name_from_expression(self, expression: str) -> Optional[str]:
        """Extract field name from a simple field expression."""
        if not expression:
            return None
        expression = expression.strip()
        if expression.startswith('[') and expression.endswith(']') and expression.count('[') == 1:
            return expression[1:-1]
        if ' ' not in expression and '(' not in expression and not any(op in expression for op in ['=', '+', '-', '*', '/']):
            return expression
        return None

    def _extract_fields_from_expression(self, expression: str) -> List[str]:
        """Extract field names from a complex expression."""
        import re
        fields = []
        if not expression:
            return fields
        bracket_fields = re.findall(r'\[([^\]]+)\]', expression)
        fields.extend(bracket_fields)
        return list(set(fields))

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

            result = self.send_request(
                "CreateSessionObject", [obj_def], handle=app_handle
            )

            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                return {
                    "error": "Failed to create hypercube for table data",
                    "response": result,
                }

            cube_handle = result["qReturn"]["qHandle"]

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
            obj_result = self.send_request("GetObject", [object_id], handle=app_handle)
            if "qReturn" not in obj_result or "qHandle" not in obj_result["qReturn"]:
                return {"error": f"Failed to get object with ID: {object_id}", "response": obj_result}

            obj_handle = obj_result["qReturn"]["qHandle"]
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

            if "qHyperCube" in obj_layout:
                hypercube = obj_layout["qHyperCube"]
                table_data = []
                dimensions = []
                measures = []
                for dim_info in hypercube.get("qDimensionInfo", []):
                    dimensions.append({
                        "title": dim_info.get("qFallbackTitle", ""),
                        "field": (dim_info.get("qGroupFieldDefs", [""])[0] if dim_info.get("qGroupFieldDefs") else ""),
                        "cardinal": dim_info.get("qCardinal", 0),
                    })
                for measure_info in hypercube.get("qMeasureInfo", []):
                    measures.append({
                        "title": measure_info.get("qFallbackTitle", ""),
                        "expression": measure_info.get("qDef", ""),
                        "format": measure_info.get("qNumFormat", {}),
                    })
                for page in hypercube.get("qDataPages", []):
                    for row in page.get("qMatrix", []):
                        row_data = {}
                        for i, cell in enumerate(row[: len(dimensions)]):
                            if i < len(dimensions):
                                row_data[f"dim_{i}_{dimensions[i]['title']}"] = {
                                    "text": cell.get("qText", ""),
                                    "numeric": (cell.get("qNum", None) if cell.get("qNum") != "NaN" else None),
                                    "state": cell.get("qState", "O"),
                                }
                        for i, cell in enumerate(row[len(dimensions) :]):
                            if i < len(measures):
                                row_data[f"measure_{i}_{measures[i]['title']}"] = {
                                    "text": cell.get("qText", ""),
                                    "numeric": (cell.get("qNum", None) if cell.get("qNum") != "NaN" else None),
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
                list_object = obj_layout["qListObject"]
                values_data = []
                for page in list_object.get("qDataPages", []):
                    for row in page.get("qMatrix", []):
                        if row and len(row) > 0:
                            cell = row[0]
                            values_data.append({
                                "value": cell.get("qText", ""),
                                "state": cell.get("qState", "O"),
                                "frequency": cell.get("qFrequency", 0),
                            })
                result["data"] = values_data
                result["structure"] = {
                    "field_name": list_object.get("qDimensionInfo", {}).get("qFallbackTitle", ""),
                    "total_values": list_object.get("qSize", {}).get("qcy", 0),
                    "returned_values": len(values_data),
                }
            elif "qPivotTable" in obj_layout:
                pivot_table = obj_layout["qPivotTable"]
                result["data"] = pivot_table.get("qDataPages", [])
                result["structure"] = {"type": "pivot_table", "size": pivot_table.get("qSize", {})}
            else:
                result["data"] = obj_layout
                result["structure"] = {"type": "unknown", "raw_layout": True}

            return result

        except Exception as e:
            return {"error": str(e), "details": "Error in get_visualization_data method"}

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
        Get comprehensive information about application for initial analysis.

        Fast overview including:
        - App metadata (size, dates, reload status)
        - Data model structure (tables, fields, types, cardinality)
        - Master items (only user-created measures and dimensions)
        - Variables (only user-created)
        - Object counts by type
        - Table relationships (key fields)

        Returns optimized JSON report for quick app understanding.
        """
        try:
            self.connect()

            # Open app once and reuse connection
            app_result = self.open_doc(app_id, no_data=False)
            if "qReturn" not in app_result or "qHandle" not in app_result["qReturn"]:
                return {"error": "Failed to open app", "response": app_result}

            app_handle = app_result["qReturn"]["qHandle"]

            # Get app metadata and layout
            app_metadata = self._get_app_metadata_fast(app_handle)

            # Get data model structure
            data_model = self._get_data_model_structure(app_handle)

            # Get master items (only user-created)
            master_items = self._get_user_master_items(app_handle)

            # Get user variables (exclude system)
            user_variables = self._get_user_variables(app_handle)

            # Get object counts by type
            object_counts = self._get_object_counts(app_handle)

            # Get table relationships
            table_relationships = self._get_table_relationships(app_handle)

            # Build optimized response
            report = {
                "app_metadata": {
                    "app_id": app_id,
                    "name": app_metadata.get("title", ""),
                    "description": app_metadata.get("description", ""),
                    "filename": app_metadata.get("filename", ""),
                    "size_bytes": app_metadata.get("size", 0),
                    "size_mb": round(app_metadata.get("size", 0) / (1024 * 1024), 2),
                    "created_date": app_metadata.get("created_date", ""),
                    "modified_date": app_metadata.get("modified_date", ""),
                    "last_reload_time": app_metadata.get("last_reload_time", ""),
                    "has_script": app_metadata.get("has_script", False),
                    "has_data": app_metadata.get("has_data", False),
                    "is_published": app_metadata.get("published", False)
                },
                "data_model": {
                    "tables": data_model.get("tables", []),
                    "total_tables": len(data_model.get("tables", [])),
                    "total_fields": sum(len(table.get("fields", [])) for table in data_model.get("tables", [])),
                    "table_relationships": table_relationships
                },
                "master_items": {
                    "measures": master_items.get("measures", []),
                    "dimensions": master_items.get("dimensions", []),
                    "total_measures": len(master_items.get("measures", [])),
                    "total_dimensions": len(master_items.get("dimensions", []))
                },
                "variables": {
                    "user_variables": user_variables,
                    "total_variables": len(user_variables)
                },
                "object_counts": object_counts,
                "reload_info": app_metadata.get("reload_info", {}),
                "summary": {
                    "analysis_type": "quick_overview",
                    "analysis_timestamp": datetime.now().isoformat()
                }
            }

            return report

        except Exception as e:
            return {"error": str(e), "details": "Error in get_app_details method"}
        finally:
            self.disconnect()

    def _get_app_metadata_fast(self, app_handle: int) -> Dict[str, Any]:
        """Get basic app metadata without heavy analysis."""
        try:
            # Get app layout
            layout_response = self.send_request("GetAppLayout", [], handle=app_handle)
            layout = layout_response.get("qLayout", {})

            # Get app properties
            properties_response = self.send_request("GetAppProperties", [], handle=app_handle)
            properties = properties_response.get("qProperties", {})

            return {
                "title": layout.get("qTitle", ""),
                "filename": layout.get("qFileName", ""),
                "description": properties.get("qMetaDef", {}).get("description", ""),
                "size": layout.get("qStaticByteSize", 0),
                "created_date": layout.get("createdDate", ""),
                "modified_date": layout.get("modifiedDate", ""),
                "last_reload_time": layout.get("qLastReloadTime", ""),
                "has_script": layout.get("qHasScript", False),
                "has_data": layout.get("qHasData", False),
                "published": layout.get("published", False),
                "reload_info": {
                    "last_execution_time": layout.get("qLastReloadTime", ""),
                    "is_partial_reload": layout.get("qIsPartialReload", False),
                    "has_data": layout.get("qHasData", False)
                }
            }
        except Exception as e:
            return {"error": str(e)}

    def _get_data_model_structure(self, app_handle: int) -> Dict[str, Any]:
        """Get tables and fields structure without usage analysis."""
        try:
            # Get tables structure using GetTablesAndKeys
            tables_result = self.send_request(
                "GetTablesAndKeys",
                [
                    {"qcx": 1000, "qcy": 1000},  # Max dimensions
                    {"qcx": 0, "qcy": 0},  # Min dimensions
                    50,  # Max tables
                    False,  # Include system tables
                    False,  # Include hidden fields
                ],
                handle=app_handle,
            )

            tables = []
            for table in tables_result.get("qtr", []):
                table_name = table.get("qName", "")
                table_fields = []

                for field in table.get("qFields", []):
                    field_info = {
                        "name": field.get("qName", ""),
                        "data_type": self._determine_data_type(field.get("qTags", [])),
                        "total_rows": field.get("qnRows", 0),
                        "distinct_values": field.get("qnTotalDistinctValues", 0),
                        "present_distinct_values": field.get("qnPresentDistinctValues", 0),
                        "completeness_pct": round(
                            (field.get("qnNonNulls", 0) / max(field.get("qnRows", 1), 1)) * 100, 1
                        ),
                        "is_key": field.get("qIsKey", False),
                        "key_type": field.get("qKeyType", "")
                    }
                    table_fields.append(field_info)

                table_info = {
                    "name": table_name,
                    "total_rows": table.get("qNoOfRows", 0),
                    "field_count": len(table_fields),
                    "fields": table_fields,
                    "is_system": table.get("qIsSystem", False),
                    "is_semantic": table.get("qIsSemantic", False)
                }
                tables.append(table_info)

            return {"tables": tables}

        except Exception as e:
            return {"error": str(e), "tables": []}

    def _determine_data_type(self, tags: List[str]) -> str:
        """Determine data type from field tags."""
        if "$numeric" in tags:
            if "$integer" in tags:
                return "integer"
            else:
                return "numeric"
        elif "$text" in tags:
            return "text"
        elif "$date" in tags:
            return "date"
        elif "$timestamp" in tags:
            return "timestamp"
        else:
            return "unknown"

    def _get_user_master_items(self, app_handle: int) -> Dict[str, Any]:
        """Get only user-created master items (exclude system)."""
        try:
            # Get master measures
            measures = self._get_master_measures(app_handle)
            user_measures = []
            for measure in measures:
                # Filter out system measures
                if not measure.get("qMeta", {}).get("qIsHidden", False):
                    user_measures.append({
                        "name": measure.get("qMeta", {}).get("title", ""),
                        "description": measure.get("qMeta", {}).get("description", ""),
                        "definition": measure.get("qMeasure", {}).get("qDef", ""),
                        "created_date": measure.get("qMeta", {}).get("createdDate", ""),
                        "modified_date": measure.get("qMeta", {}).get("modifiedDate", ""),
                        "owner": measure.get("qMeta", {}).get("owner", {}).get("name", "")
                    })

            # Get master dimensions
            dimensions = self._get_master_dimensions(app_handle)
            user_dimensions = []
            for dimension in dimensions:
                # Filter out system dimensions
                if not dimension.get("qMeta", {}).get("qIsHidden", False):
                    user_dimensions.append({
                        "name": dimension.get("qMeta", {}).get("title", ""),
                        "description": dimension.get("qMeta", {}).get("description", ""),
                        "field_definitions": dimension.get("qDim", {}).get("qFieldDefs", []),
                        "created_date": dimension.get("qMeta", {}).get("createdDate", ""),
                        "modified_date": dimension.get("qMeta", {}).get("modifiedDate", ""),
                        "owner": dimension.get("qMeta", {}).get("owner", {}).get("name", "")
                    })

            return {
                "measures": user_measures,
                "dimensions": user_dimensions
            }

        except Exception as e:
            return {"error": str(e), "measures": [], "dimensions": []}

    def _get_user_variables(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get only user-created variables (exclude system)."""
        try:
            # Create VariableList object
            variable_list_def = {
                "qInfo": {"qType": "VariableList"},
                "qVariableListDef": {
                    "qType": "variable",
                    "qShowReserved": False,  # Exclude system variables
                    "qShowConfig": False,
                    "qData": {"tags": "/tags"}
                }
            }

            variable_list_response = self.send_request("CreateSessionObject", [variable_list_def], handle=app_handle)
            if "qReturn" not in variable_list_response:
                return []

            variable_list_handle = variable_list_response["qReturn"]["qHandle"]
            layout_response = self.send_request("GetLayout", [], handle=variable_list_handle)

            variables = layout_response.get("qLayout", {}).get("qVariableList", {}).get("qItems", [])

            user_variables = []
            for variable in variables:
                # Additional filter for user variables only
                if not variable.get("qIsReserved", False) and not variable.get("qIsConfig", False):
                    user_variables.append({
                        "name": variable.get("qName", ""),
                        "definition": variable.get("qDefinition", ""),
                        "text_value": variable.get("qText", ""),
                        "numeric_value": variable.get("qNum", None),
                        "is_script_created": variable.get("qIsScriptCreated", False)
                    })

            return user_variables

        except Exception as e:
            return []

    def _get_object_counts(self, app_handle: int) -> Dict[str, int]:
        """Get count of objects by type."""
        try:
            # Get all app objects
            all_infos = self.send_request("GetAllInfos", [], handle=app_handle)

            object_counts = {}
            for info in all_infos.get("qInfos", []):
                obj_type = info.get("qType", "unknown")
                object_counts[obj_type] = object_counts.get(obj_type, 0) + 1

            # Group similar types for better readability
            grouped_counts = {
                "sheets": object_counts.get("sheet", 0),
                "charts": (
                    object_counts.get("barchart", 0) +
                    object_counts.get("linechart", 0) +
                    object_counts.get("piechart", 0) +
                    object_counts.get("combochart", 0) +
                    object_counts.get("scatterplot", 0)
                ),
                "tables": object_counts.get("table", 0),
                "kpis": object_counts.get("kpi", 0),
                "filters": (
                    object_counts.get("listbox", 0) +
                    object_counts.get("filterpane", 0)
                ),
                "text_objects": object_counts.get("text-image", 0),
                "other": sum(v for k, v in object_counts.items()
                           if k not in ["sheet", "barchart", "linechart", "piechart",
                                      "combochart", "scatterplot", "table", "kpi",
                                      "listbox", "filterpane", "text-image"])
            }

            # Add total count
            grouped_counts["total_objects"] = sum(grouped_counts.values())

            return grouped_counts

        except Exception as e:
            return {"error": str(e)}

    def _get_table_relationships(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get relationships between tables based on key fields."""
        try:
            # Get tables with key information
            tables_result = self.send_request(
                "GetTablesAndKeys",
                [
                    {"qcx": 1000, "qcy": 1000},
                    {"qcx": 0, "qcy": 0},
                    50,
                    False,
                    False
                ],
                handle=app_handle,
            )

            relationships = []
            tables = tables_result.get("qtr", [])

            # Find relationships based on field names and key types
            for i, table1 in enumerate(tables):
                table1_name = table1.get("qName", "")
                table1_keys = [f for f in table1.get("qFields", []) if f.get("qIsKey", False)]

                for j, table2 in enumerate(tables[i+1:], i+1):
                    table2_name = table2.get("qName", "")
                    table2_keys = [f for f in table2.get("qFields", []) if f.get("qIsKey", False)]

                    # Find common key fields
                    common_keys = []
                    for key1 in table1_keys:
                        for key2 in table2_keys:
                            if key1.get("qName") == key2.get("qName"):
                                common_keys.append({
                                    "field_name": key1.get("qName"),
                                    "key_type": key1.get("qKeyType", "")
                                })

                    if common_keys:
                        relationships.append({
                            "table1": table1_name,
                            "table2": table2_name,
                            "relationship_type": "key_match",
                            "common_fields": common_keys
                        })

            return relationships

        except Exception as e:
            return []
