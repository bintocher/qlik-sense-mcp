"""Sheets and the objects on them, plus the fields those objects use."""

from typing import Dict, List, Any, Optional
import logging

from ..exceptions import QlikEngineError

logger = logging.getLogger(__name__)

# Words that appear where a field name could, but never name a field:
# operators, set-analysis syntax, constants, and the aggregation modifiers
# that are written without parentheses.
_EXPRESSION_KEYWORDS = frozenset("""
    and or not xor like precedes follows
    if then else elseif end
    total distinct all aggr
    null true false
    as in when
    intervalmatch resident inline autogenerate
    e pi
""".split())


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
        """Extract field names used in an object layout.

        A hypercube carries a *list* of qDimensionInfo, a list object exactly
        one — Engine's own shapes, not a quirk of some charts. Iterating the
        list object's as a list walked the dict's keys and raised
        `'str' object has no attribute 'get'`, which the catch-all below
        turned into a warning and an empty field list. Filter panes are the
        objects built on list objects, so every filter on every sheet came
        back as "uses no fields".
        """
        fields = set()

        def add_dimension(dim_info: Any) -> None:
            if not isinstance(dim_info, dict):
                return
            for field_def in dim_info.get("qGroupFieldDefs", []):
                field_name = self._extract_field_name_from_expression(field_def)
                if field_name:
                    fields.add(field_name)
                else:
                    # A calculated dimension, e.g. "=Year(OrderDate)": the
                    # simple form finds nothing, but the fields inside it are
                    # exactly what the caller asked about.
                    fields.update(self._extract_fields_from_expression(field_def))

        try:
            hypercube = obj_layout.get("qHyperCube")
            if isinstance(hypercube, dict):
                for dim_info in hypercube.get("qDimensionInfo", []):
                    add_dimension(dim_info)
                for measure_info in hypercube.get("qMeasureInfo", []):
                    if isinstance(measure_info, dict):
                        fields.update(
                            self._extract_fields_from_expression(measure_info.get("qDef", ""))
                        )

            list_obj = obj_layout.get("qListObject")
            if isinstance(list_obj, dict):
                dim_info = list_obj.get("qDimensionInfo")
                # Tolerate a list too: some object types nest several.
                for entry in (dim_info if isinstance(dim_info, list) else [dim_info]):
                    add_dimension(entry)

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
        """Extract field names from a Qlik expression.

        Bracketed names are unambiguous. Bare ones are not, so the rule is
        structural: an identifier followed by `(` is a function call, and
        everything else that is not a keyword is taken as a field. Matching
        only `[...]` — as this did — meant `Sum(Sales)` reported no fields at
        all, and most real measures are written without brackets.

        This is a lexical guess, not a parser: an unusual function name would
        be reported as a field. That is the tolerable direction of error for
        "which fields does this object touch".
        """
        import re

        if not expression:
            return []

        fields = set(re.findall(r"\[([^\]]+)\]", expression))

        # Drop string literals first — 'Sales' inside a set expression is a
        # value, not a field.
        without_literals = re.sub(r"'[^']*'", " ", expression)
        # And bracketed names, already collected above.
        without_literals = re.sub(r"\[[^\]]*\]", " ", without_literals)

        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*(\()?", without_literals):
            name, is_call = match.group(1), match.group(2)
            if is_call or name.lower() in _EXPRESSION_KEYWORDS:
                continue
            fields.add(name)

        return sorted(fields)

