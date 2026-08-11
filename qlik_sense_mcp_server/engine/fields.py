"""Field-level reads: values, ranges, statistics, descriptions."""


from typing import Dict, List, Any
import logging

from ..exceptions import QlikEngineError
import uuid

logger = logging.getLogger(__name__)


class EngineFieldsMixin:
    def get_fields(self, app_handle: int) -> List[Dict[str, Any]]:
        """Get app fields using GetTablesAndKeys method."""
        try:
            # Use correct GetTablesAndKeys method as in qsea.py
            result = self.send_request(
                "GetTablesAndKeys",
                [
                    {"qcx": 1000, "qcy": 1000},  # Max dimensions
                    {"qcx": 0, "qcy": 0},  # Min dimensions
                    30,  # qCellHeight
                    True,  # qSyntheticMode
                    False,  # qIncludeSysVars
                ],
                handle=app_handle,
            )


            fields_info = []

            if "qtr" in result:
                for table in result["qtr"]:
                    table_name = table.get("qName", "Unknown")
                    table_comment = table.get("qComment", "")

                    if "qFields" in table:
                        for field in table["qFields"]:
                            field_info = {
                                "field_name": field.get("qName", ""),
                                "table_name": table_name,
                                # COMMENT FIELD / COMMENT TABLE from the load
                                # script — the only human description a field
                                # carries in the data model.
                                "comment": field.get("qComment", ""),
                                "table_comment": table_comment,
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

    SCHEMA_CACHE_TTL_SECONDS = 600.0

    def _schema_store(self) -> Dict[str, Dict[str, Any]]:
        """Per-instance cache, created on first use.

        Deliberately not a class attribute: instances built without
        __init__ (test doubles, subclasses) would then share one dict and
        answer each other's questions.
        """
        store = self.__dict__.get("_schema_cache")
        if store is None:
            store = {}
            self.__dict__["_schema_cache"] = store
        return store

    def cached_fields(self, app_handle: int, app_id: str,
                      reload_stamp: Any = None) -> Dict[str, Any]:
        """`get_fields`, remembered per app.

        Reading the data model costs a `GetTablesAndKeys` round-trip and
        the answer only changes when the app reloads — but every field
        check, every suggestion and every `get_app_details` was paying for
        it again. The entry is dropped when the reload timestamp moves, and
        expires anyway after ten minutes so a stale entry cannot outlive a
        reload the caller never mentioned.
        """
        import time as _time
        store = self._schema_store()
        entry = store.get(app_id)
        fresh = (
            entry is not None
            and entry.get("reload_stamp") == reload_stamp
            and (_time.monotonic() - entry["read_at"]) < self.SCHEMA_CACHE_TTL_SECONDS
        )
        if fresh:
            entry["hits"] = entry.get("hits", 0) + 1
            return entry["model"]

        model = self.get_fields(app_handle)
        if isinstance(model, dict) and model.get("fields"):
            store[app_id] = {
                "model": model, "reload_stamp": reload_stamp,
                "read_at": _time.monotonic(), "hits": 0,
            }
        return model

    def forget_schema(self, app_id: str = None) -> None:
        """Drop one app's cached model, or all of them."""
        store = self._schema_store()
        if app_id is None:
            store.clear()
        else:
            store.pop(app_id, None)

    def search_app(self, app_handle: int, term: str, fields: List[str] = None,
                   max_fields: int = 8, max_values: int = 5) -> Dict[str, Any]:
        """Find a term among the app's values, without knowing the field.

        Engine's own `SearchResults` does this. Measured on a 10M-row app:
        across every field it takes about 30 seconds, against a named list
        of fields it is instant — so the caller can narrow it when it knows
        roughly where to look, and pay the full scan when it does not.
        """
        search_fields = fields or []
        result = self.send_request(
            "SearchResults",
            [
                {"qSearchFields": search_fields, "qContext": "CurrentSelections"},
                [term],
                {"qOffset": 0, "qCount": max_fields,
                 "qMaxNbrFieldMatches": max_values},
            ],
            handle=app_handle,
            timeout=self.ws_operation_timeout,
        )
        groups = ((result.get("qResult") or {}).get("qSearchGroupArray") or [])
        matches = []
        for group in groups:
            for item in group.get("qItems", []) or []:
                values = [m.get("qText", "") for m in item.get("qItemMatches", []) or []]
                if values:
                    matches.append({
                        "field": item.get("qIdentifier", ""),
                        "values": values,
                        "total_matches_in_field": item.get("qItemMatches") and
                                                  item.get("qTotalNumberOfMatches",
                                                           len(values)),
                    })
        return {"term": term, "matches": matches, "fields_matched": len(matches)}

    def get_field_description(self, app_handle: int, field_name: str) -> Dict[str, Any]:
        """Describe one field via `GetFieldDescription`.

        The only Engine call that returns a single field's `COMMENT FIELD`
        text without walking the whole data model. Cheap — no hypercube, no
        data page. Returns `{}` when the field is unknown to the model
        (Engine answers "Invalid parameters" in that case).
        """
        try:
            result = self.send_request(
                "GetFieldDescription", [field_name], handle=app_handle
            )
            info = result.get("qReturn", {}) or {}
            return {
                "name": info.get("qName", field_name),
                "comment": info.get("qComment", ""),
                "src_tables": info.get("qSrcTables", []),
                "cardinal": info.get("qCardinal", 0),
                "total_count": info.get("qTotalCount", 0),
                "is_numeric": info.get("qIsNumeric", False),
                "tags": info.get("qTags", []),
                "byte_size": info.get("qByteSize", 0),
            }
        except Exception as e:
            logger.debug("GetFieldDescription(%s) failed: %s", field_name, e)
            return {}

    def search_field_values(self, app_handle: int, field_name: str,
                            pattern: str, limit: int, offset: int = 0,
                            case_sensitive: bool = False) -> Dict[str, Any]:
        """Distinct values of a field matching a wildcard, filtered by Engine.

        Filtering here rather than in the caller is the whole point. Reading
        the first N values and matching them locally can only ever find
        matches inside that prefix: on a field with 200k distinct values, a
        match at position 150k does not exist as far as the caller is
        concerned, and no part of the reply says so.

        The filter is a calculated dimension — non-matching values evaluate
        to NULL and are suppressed — so paging applies to the matches, not
        to the field.
        """
        # `%` is accepted as a wildcard alongside `*` for consistency with
        # the other filters in this server; Qlik only knows `*` and `?`.
        qlik_pattern = pattern.replace("%", "*")
        escaped = qlik_pattern.replace("'", "''")

        # Qlik has no case-sensitive wildcard function. Checked on 31.62:
        # Match() respects case but takes no wildcards
        # (Match('C000001','C00000*') = 0), WildMatch() takes wildcards but
        # ignores case (WildMatch('abc','ABC') = 1). A case-sensitive search
        # therefore selects the superset with WildMatch and narrows it here:
        # the case-sensitive answer is always a subset of the
        # case-insensitive one, so nothing can be missed this way.
        expression = f"=If(WildMatch([{field_name}], '{escaped}'), [{field_name}])"
        definition = {
            "qInfo": {"qId": f"field-search-{field_name}", "qType": "HyperCube"},
            "qHyperCubeDef": {
                "qDimensions": [{
                    "qDef": {"qFieldDefs": [expression]},
                    "qNullSuppression": True,
                }],
                "qMeasures": [],
                "qInitialDataFetch": [{"qTop": 0, "qLeft": 0, "qHeight": 1, "qWidth": 1}],
                "qSuppressZero": False,
                "qSuppressMissing": False,
                "qMode": "S",
                "qInterColumnSortOrder": [0],
            },
        }

        offset = max(0, offset)
        limit = max(1, limit)

        with self.session_object(app_handle, definition) as cube_handle:
            layout = self.send_request("GetLayout", [], handle=cube_handle)
            hypercube = (layout.get("qLayout") or {}).get("qHyperCube")
            if hypercube is None:
                raise QlikEngineError(f"No hypercube in search layout: {layout}")
            total_matches = hypercube.get("qSize", {}).get("qcy", 0)

            if not case_sensitive:
                values = self._read_search_page(cube_handle, offset, limit)
                return {
                    "values": [{"value": v} for v in values],
                    "total_matches": total_matches,
                    "search_applied": pattern,
                }

            # Case-sensitive: walk the superset in pages, keep the exact
            # matches, stop as soon as the requested page is filled.
            case_matcher = _wildcard_matcher(qlik_pattern)
            kept: List[str] = []
            scanned = 0
            page_size = max(limit * 4, 200)
            hit_scan_cap = False
            while scanned < total_matches:
                if scanned >= self.MAX_CASE_SENSITIVE_SCAN:
                    hit_scan_cap = True
                    break
                chunk = self._read_search_page(
                    cube_handle, scanned, min(page_size, total_matches - scanned))
                if not chunk:
                    break
                scanned += len(chunk)
                kept.extend(v for v in chunk if case_matcher(v))
                if len(kept) >= offset + limit:
                    break

            result: Dict[str, Any] = {
                "values": [{"value": v} for v in kept[offset:offset + limit]],
                "search_applied": pattern,
                "case_sensitive": True,
                "candidates_scanned": scanned,
            }
            if scanned >= total_matches and not hit_scan_cap:
                # The whole superset was read, so this count is exact.
                result["total_matches"] = len(kept)
            else:
                # Stopped early — say so rather than quote a count that
                # only covers the part that was read.
                result["total_matches_at_least"] = len(kept)
                result["search_truncated"] = hit_scan_cap
            return result

    # Case-sensitive matching happens here, not in Qlik, so a search has to
    # read candidates. This caps how many, to stop one search from walking
    # a 200k-value field end to end.
    MAX_CASE_SENSITIVE_SCAN = 20000

    def _read_search_page(self, cube_handle: int, top: int, height: int) -> List[str]:
        """One page of the search hypercube's single column."""
        if height <= 0:
            return []
        reply = self.send_request(
            "GetHyperCubeData",
            ["/qHyperCubeDef", [{"qTop": top, "qLeft": 0,
                                 "qHeight": height, "qWidth": 1}]],
            handle=cube_handle,
        )
        return [row[0].get("qText", "")
                for page in reply.get("qDataPages", []) or []
                for row in page.get("qMatrix", [])]

    def get_field_values(
        self,
        app_handle: int,
        field_name: str,
        max_values: int = 100,
        include_frequency: bool = True,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """Distinct values of a field, read through a ListObject.

        `offset` is passed to Engine as the page top, so paging walks the
        field itself rather than a prefetched prefix of it.
        """
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
                    {"qTop": max(0, offset), "qLeft": 0,
                     "qHeight": max_values, "qWidth": 1}
                ],
            },
        }

        try:
            # session_object destroys the object on every path, including
            # the exception ones the previous code walked straight past.
            with self.session_object(app_handle, list_def) as list_handle:
                layout = self.send_request("GetLayout", [], handle=list_handle)
                list_object = (layout.get("qLayout") or {}).get("qListObject")
                if list_object is None:
                    return {"error": "No list object in layout", "layout": layout}

                values = []
                for page in list_object.get("qDataPages", []):
                    for row in page.get("qMatrix", []):
                        for cell in row:
                            values.append({
                                "value": cell.get("qText", ""),
                                "numeric_value": (cell.get("qNum")
                                                  if cell.get("qNum") != "NaN" else None),
                                "frequency": cell.get("qFrequency", ""),
                                "state": cell.get("qState", ""),
                            })

                result = {
                    "field_name": field_name,
                    "values": values,
                    "total_values": list_object.get("qSize", {}).get("qcy", 0),
                    "returned_values": len(values),
                }

                if not values:
                    # A high-cardinality field can come back empty from the
                    # ListObject path; a single-dimension hypercube still
                    # materialises the values.
                    fallback = self._get_field_values_via_hypercube(
                        app_handle, field_name, max_values)
                    if fallback.get("values"):
                        fallback["fallback_used"] = "hypercube"
                        return fallback
                    result["warning"] = (
                        "Neither the ListObject nor the hypercube fallback "
                        "returned values for this field.")
                return result
        except Exception as e:
            return {"error": str(e), "details": "Error in get_field_values method"}

    def get_field_values_batch(
        self, app_handle: int, wanted: List[Any]
    ) -> Dict[str, List[str]]:
        """Distinct values for several fields in a fixed number of round-trips.

        `wanted` is a list of `(field_name, how_many)`. Reading the fields
        one at a time would cost three round-trips each — create, read,
        destroy — which is why the caller used to skip reading them at all
        and leave the values to be guessed.

        Best-effort by design: a field that fails is simply absent from the
        result. This feeds a convenience section of a reply, and must never
        be the reason the reply fails.
        """
        if not wanted:
            return {}

        objects = []
        for field_name, how_many in wanted:
            object_id = f"sample-{uuid.uuid4().hex[:12]}"
            objects.append((field_name, object_id, {
                "qInfo": {"qId": object_id, "qType": "ListObject"},
                "qListObjectDef": {
                    "qDef": {"qFieldDefs": [field_name]},
                    "qInitialDataFetch": [
                        {"qTop": 0, "qLeft": 0, "qHeight": how_many, "qWidth": 1}],
                },
            }))

        created = self.send_requests_pipelined(
            [{"method": "CreateSessionObject", "params": [definition], "handle": app_handle}
             for _name, _oid, definition in objects],
            raise_on_error=False,
        )
        handles = []
        for outcome in created:
            if isinstance(outcome, Exception):
                handles.append(None)
                continue
            handles.append(((outcome or {}).get("qReturn") or {}).get("qHandle"))

        live = [i for i, handle in enumerate(handles) if handle is not None]
        layouts = self.send_requests_pipelined(
            [{"method": "GetLayout", "params": [], "handle": handles[i]} for i in live],
            raise_on_error=False,
        ) if live else []

        values: Dict[str, List[str]] = {}
        for index, layout in zip(live, layouts):
            if isinstance(layout, Exception):
                continue
            list_object = ((layout or {}).get("qLayout") or {}).get("qListObject") or {}
            collected = [
                cell.get("qText", "")
                for page in list_object.get("qDataPages", []) or []
                for row in page.get("qMatrix", []) or []
                for cell in row
                if cell.get("qText", "") != ""
            ]
            if collected:
                values[objects[index][0]] = collected

        # Session objects hold their result set in Engine memory until they
        # are destroyed, so clean up even though nothing here failed.
        try:
            self.send_requests_pipelined(
                [{"method": "DestroySessionObject", "params": [objects[i][1]],
                  "handle": app_handle} for i in live],
                raise_on_error=False,
            )
        except Exception as exc:
            logger.warning("Could not destroy sample list objects: %s", exc)

        return values

    def get_field_edges_batch(
        self, app_handle: int, field_names: List[str], count: int = 5
    ) -> Dict[str, Dict[str, List[str]]]:
        """The lowest and highest values of each field, in one pass.

        For a field with thousands of distinct values, listing them is
        useless and omitting them leaves the caller guessing the shape of
        the data. The two ends answer what actually gets asked: what does
        a value look like, and what range is it in. Sorted by Qlik, so
        text sorts as text and numbers as numbers.
        """
        if not field_names:
            return {}

        specs = []  # (field, end, object_id, definition)
        for name in field_names:
            for end, direction in (("lowest", 1), ("highest", -1)):
                object_id = f"edge-{uuid.uuid4().hex[:12]}"
                specs.append((name, end, object_id, {
                    "qInfo": {"qId": object_id, "qType": "ListObject"},
                    "qListObjectDef": {
                        "qDef": {
                            "qFieldDefs": [name],
                            "qSortCriterias": [{
                                "qSortByNumeric": direction,
                                "qSortByAscii": direction,
                                "qSortByLoadOrder": 0,
                                "qSortByFrequency": 0,
                                "qSortByState": 0,
                                "qSortByExpression": 0,
                                "qExpression": {"qv": ""},
                            }],
                        },
                        "qInitialDataFetch": [
                            {"qTop": 0, "qLeft": 0, "qHeight": count, "qWidth": 1}],
                    },
                }))

        try:
            created = self.send_requests_pipelined(
                [{"method": "CreateSessionObject", "params": [definition],
                  "handle": app_handle} for _n, _e, _oid, definition in specs],
                raise_on_error=False,
            )
        except Exception as exc:
            logger.debug("Could not read field edges: %s", exc)
            return {}

        handles = []
        for outcome in created:
            if isinstance(outcome, Exception):
                handles.append(None)
                continue
            handles.append(((outcome or {}).get("qReturn") or {}).get("qHandle"))

        live = [i for i, handle in enumerate(handles) if handle is not None]
        try:
            layouts = self.send_requests_pipelined(
                [{"method": "GetLayout", "params": [], "handle": handles[i]}
                 for i in live],
                raise_on_error=False,
            ) if live else []
        except Exception as exc:
            logger.debug("Could not read field-edge layouts: %s", exc)
            return {}

        edges: Dict[str, Dict[str, List[str]]] = {}
        for index, layout in zip(live, layouts):
            if isinstance(layout, Exception):
                continue
            name, end, _oid, _definition = specs[index]
            list_object = ((layout or {}).get("qLayout") or {}).get("qListObject") or {}
            values = [
                cell.get("qText", "")
                for page in list_object.get("qDataPages", []) or []
                for row in page.get("qMatrix", []) or []
                for cell in row
                if cell.get("qText", "") != ""
            ]
            if values:
                edges.setdefault(name, {})[end] = values

        try:
            self.send_requests_pipelined(
                [{"method": "DestroySessionObject", "params": [specs[i][2]],
                  "handle": app_handle} for i in live],
                raise_on_error=False,
            )
        except Exception as exc:
            logger.warning("Could not destroy edge list objects: %s", exc)

        return edges

    def _get_field_values_via_hypercube(
        self, app_handle: int, field_name: str, max_values: int = 100
    ) -> Dict[str, Any]:
        """
        Fallback for `get_field_values`: build a one-dimension hypercube
        with a Count() measure to materialize distinct field values when
        ListObject returns empty.
        """
        try:
            obj_id = f"field-values-fb-{field_name}"
            hypercube_def = {
                "qDimensions": [
                    {
                        "qDef": {
                            "qFieldDefs": [field_name],
                            "qSortCriterias": [
                                {
                                    "qSortByNumeric": -1,
                                    "qSortByAscii": 1,
                                    "qSortByLoadOrder": 1,
                                    "qSortByExpression": 0,
                                    "qExpression": {"qv": ""},
                                }
                            ],
                        },
                        "qNullSuppression": True,
                        "qIncludeElemValue": True,
                    }
                ],
                "qMeasures": [
                    {"qDef": {"qDef": f"Count([{field_name}])", "qLabel": "cnt"}}
                ],
                "qInitialDataFetch": [
                    {"qTop": 0, "qLeft": 0, "qHeight": max_values, "qWidth": 2}
                ],
                "qSuppressZero": False,
                "qSuppressMissing": False,
                "qMode": "S",
            }
            obj_def = {
                "qInfo": {"qId": obj_id, "qType": "HyperCube"},
                "qHyperCubeDef": hypercube_def,
            }
            result = self.send_request(
                "CreateSessionObject", [obj_def], handle=app_handle,
                timeout=self.ws_operation_timeout,
            )
            if "qReturn" not in result or "qHandle" not in result["qReturn"]:
                return {"error": "fallback hypercube create failed", "values": []}
            cube_handle = result["qReturn"]["qHandle"]
            layout = self.send_request(
                "GetLayout", [], handle=cube_handle,
                timeout=self.ws_operation_timeout,
            )
            values_data: List[Dict[str, Any]] = []
            try:
                hc = layout["qLayout"]["qHyperCube"]
                for page in hc.get("qDataPages", []):
                    for row in page.get("qMatrix", []):
                        if not row:
                            continue
                        cell = row[0]
                        values_data.append({
                            "value": cell.get("qText", ""),
                            "state": cell.get("qState", "O"),
                            "numeric_value": cell.get("qNum", None),
                            "is_numeric": cell.get("qIsNumeric", False),
                            "frequency": int(row[1].get("qNum", 0)) if len(row) > 1 else 0,
                        })
                total_values = hc.get("qSize", {}).get("qcy", len(values_data))
            except Exception:
                total_values = len(values_data)
            try:
                self.send_request(
                    "DestroySessionObject", [obj_id], handle=app_handle
                )
            except Exception:
                pass
            return {
                "field_name": field_name,
                "values": values_data,
                "total_values": total_values,
                "returned_count": len(values_data),
            }
        except Exception as e:
            return {"error": str(e), "values": []}

    def get_field_range(self, app_handle: int, field_name: str) -> Dict[str, Any]:
        """
        Lightweight bounds query: distinct count + min + max for a single field.

        Builds a measures-only hypercube with 3 expressions, no dimensions.
        Engine resolves Min/Max from the symbol table without scanning rows,
        so this runs in seconds even on billion-row tables.
        """
        try:
            exprs = [
                (f"Count(DISTINCT [{field_name}])", "unique_values"),
                (f"Min([{field_name}])", "min_value"),
                (f"Max([{field_name}])", "max_value"),
            ]
            hypercube_def = {
                "qDimensions": [],
                "qMeasures": [
                    {"qDef": {"qDef": expr, "qLabel": label}}
                    for expr, label in exprs
                ],
                "qInitialDataFetch": [
                    {"qTop": 0, "qLeft": 0, "qHeight": 1, "qWidth": len(exprs)}
                ],
                "qSuppressZero": False,
                "qSuppressMissing": False,
            }
            obj_def = {
                "qInfo": {"qId": f"field-range-{field_name}", "qType": "HyperCube"},
                "qHyperCubeDef": hypercube_def,
            }
            # session_object destroys the object on every path out — the
            # early return below used to skip the cleanup written after it.
            with self.session_object(app_handle, obj_def,
                                     timeout=self.ws_operation_timeout) as cube_handle:
                layout = self.send_request("GetLayout", [], handle=cube_handle,
                                           timeout=self.ws_operation_timeout)
                hypercube = (layout.get("qLayout") or {}).get("qHyperCube")
                if hypercube is None:
                    return {"error": "No hypercube in layout", "layout": layout}
                stats: Dict[str, Any] = {"field_name": field_name}
                for page in hypercube.get("qDataPages", []):
                    for row in page.get("qMatrix", []):
                        for i, cell in enumerate(row):
                            if i < len(exprs):
                                label = exprs[i][1]
                                stats[label] = {
                                    "text": cell.get("qText", ""),
                                    "numeric": (cell.get("qNum", None)
                                                if cell.get("qNum") != "NaN" else None),
                                    "is_numeric": cell.get("qIsNumeric", False),
                                }
                return stats
        except Exception as e:
            import traceback
            return {
                "error": str(e),
                "details": "Error in get_field_range method",
                "traceback": traceback.format_exc(),
            }

    def get_field_statistics(self, app_handle: int, field_name: str,
                             light: bool = True) -> Dict[str, Any]:
        """
        Compute statistics for a field via a measures-only hypercube.

        Args:
            app_handle: Open app handle.
            field_name: Field name (no square brackets).
            light: When True (default), only compute Count, DISTINCT, non-null,
                Min, Max — fast on any table size. When False, additionally
                compute Avg, Sum, Median, Mode, Stdev — these can be VERY slow
                on big fact tables (>100M rows) and meaningless on dates/text.
        """
        debug_log = [f"get_field_statistics(app_handle={app_handle}, "
                     f"field_name={field_name}, light={light})"]
        try:
            # Count() ignores NULLs, so it is the non-null count and nothing
            # else; the total has to add NullCount() back. The previous
            # expressions named Count() "total_count" and paired it with
            # `Count({$<[field]={'*'}>})` — an aggregation with no argument —
            # so the derived null percentage compared two counts of the same
            # non-null values and came out at ~0 no matter how much was
            # missing. Checked against a 10M-row app: Count 9,500,082 +
            # NullCount 499,918 = 10,000,000 rows exactly.
            stats_expressions = [
                f"Count(DISTINCT [{field_name}])",  # Unique values
                f"Count([{field_name}])",           # Non-null count
                f"NullCount([{field_name}])",       # Rows where the field is NULL
                f"Min([{field_name}])",  # Minimum value
                f"Max([{field_name}])",  # Maximum value
            ]
            stats_labels = [
                "unique_values",
                "non_null_count",
                "null_count",
                "min_value",
                "max_value",
            ]
            if not light:
                stats_expressions.extend([
                    f"Avg([{field_name}])",
                    f"Sum([{field_name}])",
                    f"Median([{field_name}])",
                    f"Mode([{field_name}])",
                    f"Stdev([{field_name}])",
                ])
                stats_labels.extend([
                    "avg_value",
                    "sum_value",
                    "median_value",
                    "mode_value",
                    "std_deviation",
                ])

            obj_def = {
                "qInfo": {"qId": f"field-stats-{field_name}", "qType": "HyperCube"},
                "qHyperCubeDef": {
                    "qDimensions": [],
                    "qMeasures": [
                        {"qDef": {"qDef": expr, "qLabel": f"Stat_{i}"}}
                        for i, expr in enumerate(stats_expressions)
                    ],
                    "qInitialDataFetch": [
                        {"qTop": 0, "qLeft": 0, "qHeight": 1,
                         "qWidth": len(stats_expressions)}
                    ],
                    "qSuppressZero": False,
                    "qSuppressMissing": False,
                },
            }

            with self.session_object(app_handle, obj_def,
                                     timeout=self.ws_operation_timeout) as cube_handle:
                layout = self.send_request("GetLayout", [], handle=cube_handle,
                                           timeout=self.ws_operation_timeout)
                hypercube = (layout.get("qLayout") or {}).get("qHyperCube")
                if hypercube is None:
                    return {"error": "No hypercube in statistics layout",
                            "layout": layout, "debug_log": debug_log}

                statistics: Dict[str, Any] = {"field_name": field_name}
                for page in hypercube.get("qDataPages", []):
                    for row in page.get("qMatrix", []):
                        for i, cell in enumerate(row):
                            if i < len(stats_labels):
                                statistics[stats_labels[i]] = {
                                    "text": cell.get("qText", ""),
                                    "numeric": (cell.get("qNum")
                                                if cell.get("qNum") != "NaN" else None),
                                    "is_numeric": cell.get("qIsNumeric", False),
                                }

                def _numeric(key):
                    value = statistics.get(key, {}).get("numeric")
                    return value if isinstance(value, (int, float)) else 0

                non_null = _numeric("non_null_count")
                nulls = _numeric("null_count")
                total = non_null + nulls
                # Rows the field appears in, NULLs included — the only
                # denominator that makes "how complete is this column?"
                # answerable. Reported explicitly so the caller does not
                # have to work out which count is which.
                statistics["total_count"] = {
                    "text": str(int(total)),
                    "numeric": total,
                    "is_numeric": True,
                }
                if total > 0:
                    statistics["null_percentage"] = round(nulls / total * 100, 2)
                    statistics["completeness_percentage"] = round(
                        non_null / total * 100, 2)
                statistics["debug_log"] = debug_log
                return statistics

        except Exception as e:
            return {"error": str(e), "details": "Error in get_field_statistics method",
                    "debug_log": debug_log}



def _wildcard_matcher(pattern: str):
    """Compile Qlik's wildcard syntax into a case-sensitive matcher.

    Only `*` and `?` are wildcards in Qlik; every other character is
    literal. `fnmatch` cannot be used directly for this — it also reads
    `[...]` as a character class, so a value like `Order[1]` would match
    the pattern `Order[12]`, which in Qlik it does not.
    """
    import re as _re

    parts = []
    for char in pattern:
        if char == "*":
            parts.append(".*")
        elif char == "?":
            parts.append(".")
        else:
            parts.append(_re.escape(char))
    compiled = _re.compile("".join(parts) + r"\Z")

    def _matches(value: str) -> bool:
        return compiled.match(value or "") is not None

    return _matches
