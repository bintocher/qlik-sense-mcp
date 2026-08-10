"""Sheets and the objects on them, plus the fields those objects use."""

from typing import Dict, List, Any, Optional
import logging

from ..exceptions import QlikEngineError

logger = logging.getLogger(__name__)


class EngineSheetsMixin:
    def get_script(self, app_handle: int) -> str:
        """Get load script."""
        result = self.send_request("GetScript", [], handle=app_handle)
        return result.get("qScript", "")

    def get_sheets(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get app sheets.

        Raises rather than returning an empty list on failure: "this app
        has no sheets" and "the Engine call failed" are different answers,
        and the caller cannot act on them the same way.
        """
        sheet_list_def = {
            "qInfo": {"qId": "SheetList", "qType": "SheetList"},
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

        with self.session_object(app_handle, sheet_list_def) as list_handle:
            layout_result = self.send_request("GetLayout", [], handle=list_handle)
            object_list = (layout_result.get("qLayout") or {}).get("qAppObjectList")
            if object_list is None:
                raise QlikEngineError(
                    f"No sheet list in layout for app handle {app_handle}: "
                    f"{layout_result}")
            sheets = object_list.get("qItems", [])
            logger.info("Found %d sheets", len(sheets))
            return sheets

    def _get_sheet_objects_detailed(self, app_handle: int, sheet_id: str) -> List[Dict[str, Any]]:
        """Get detailed information about objects on a sheet.

        Fetches all child objects' handles and layouts in two pipelined
        batches (see `send_requests_pipelined`) instead of one
        GetObject+GetLayout round-trip per object — a sheet with N objects
        used to cost 2N sequential round-trips, now costs 2 regardless of N.
        Per-object failures are still isolated: one bad object is skipped,
        the rest of the sheet is unaffected, same as before.
        """
        # A failure to reach the sheet itself is reported; a failure on one
        # of its objects is not, because the other objects are still worth
        # returning. Those two cases are what the try/except split below is.
        sheet_result = self.send_request("GetObject", {"qId": sheet_id}, handle=app_handle)
        sheet_handle = (sheet_result.get("qReturn") or {}).get("qHandle")
        if sheet_handle is None:
            raise QlikEngineError(
                f"No sheet {sheet_id!r} in this app: {sheet_result}")

        sheet_layout = self.send_request("GetLayout", [], handle=sheet_handle)
        child_list = (sheet_layout.get("qLayout") or {}).get("qChildList")
        if child_list is None:
            logger.info("Sheet %s has no child objects", sheet_id)
            return []

        try:
            child_objects = child_list.get("qItems", [])
            child_meta = [
                (co.get("qInfo", {}).get("qId", ""), co.get("qInfo", {}).get("qType", ""), co)
                for co in child_objects
            ]
            child_meta = [(obj_id, obj_type, co) for obj_id, obj_type, co in child_meta if obj_id]
            if not child_meta:
                return []

            # Wave 1: resolve every child object's handle in one pipelined
            # round-trip instead of N sequential GetObject calls.
            get_object_outcomes = self.send_requests_pipelined(
                [{"method": "GetObject", "params": {"qId": obj_id}, "handle": app_handle}
                 for obj_id, _obj_type, _co in child_meta],
                raise_on_error=False,
            )

            obj_handles: List[Optional[int]] = []
            for outcome in get_object_outcomes:
                if (isinstance(outcome, Exception)
                        or "qReturn" not in outcome or "qHandle" not in outcome["qReturn"]):
                    obj_handles.append(None)
                else:
                    obj_handles.append(outcome["qReturn"]["qHandle"])

            # Wave 2: fetch every resolved object's layout in one more
            # pipelined round-trip instead of N sequential GetLayout calls.
            layout_indices = [i for i, h in enumerate(obj_handles) if h is not None]
            layout_outcomes = self.send_requests_pipelined(
                [{"method": "GetLayout", "params": [], "handle": obj_handles[i]}
                 for i in layout_indices],
                raise_on_error=False,
            ) if layout_indices else []
            layout_by_index = dict(zip(layout_indices, layout_outcomes))

            detailed_objects = []
            for i, (obj_id, obj_type, child_obj) in enumerate(child_meta):
                obj_layout = layout_by_index.get(i)
                if obj_layout is None:
                    continue  # GetObject failed for this child — same as the old `continue`
                if isinstance(obj_layout, Exception):
                    logger.warning(f"Error processing object {obj_id}: {obj_layout}")
                    continue
                if "qLayout" not in obj_layout:
                    continue
                try:
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
            logger.error("_get_sheet_objects_detailed error: %s", e)
            raise

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

