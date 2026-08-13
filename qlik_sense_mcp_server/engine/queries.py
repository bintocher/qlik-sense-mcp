"""Typed queries: say what to group and what to measure, not how.

A caller states fields, aggregations and filters. This module writes the
Qlik expressions, proves the filters select something, runs every query in
the batch over the same three round-trips, and returns the control values
that show whether the filter did what was asked.

Set analysis written by hand stays available through
`engine_create_hypercube`. It is the harder path: measured across ten
models, every wrong answer with a known-correct value came from a
hand-written filter on a date, and Qlik answers such a filter with a
plausible number rather than an error.
"""

import json
import logging
import time
import uuid

from ..utils import bare_field_name, escape_qlik_field_name
from .filters import _set_identifier
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


MAX_NESTING = 20

# How many times a short page is read on before the reply says so.
MAX_PAGE_READS = 5

# What one measure may grow to. A division writes its denominator
# twice, so nesting them doubles the text at every level.
MAX_EXPRESSION_CHARS = 20000

# A reference count plus the candidate forms of a date field.
RANGE_PROBE_COST = 3


def _yes_or_no(value: Any, key: str, query_id: str
               ) -> Optional[Dict[str, Any]]:
    """The refusal a non-boolean earns for a key that means yes or no."""
    if value is None or isinstance(value, bool):
        return None
    return {"id": query_id, "error": f"{key}={value!r} is not yes or no.",
            "error_category": "invalid_argument",
            "hint": "true or false, not the word for it."}


def _too_long(expression: str, query_id: str) -> Optional[Dict[str, Any]]:
    """The refusal an oversized expression earns, or nothing.

    One expression has a size, whichever shape built it. A division writes
    its denominator twice - the second time to guard against zero - so
    divisions inside divisions double the text at every level; a measure
    written by hand is as long as it was written.
    """
    if len(expression) <= MAX_EXPRESSION_CHARS:
        return None
    return {"id": query_id, "error": (
        f"This measure is {len(expression)} characters long, over the "
        f"{MAX_EXPRESSION_CHARS} this server sends."),
        "error_category": "limit_exceeded",
        "hint": ("A division writes its denominator twice, so divisions "
                 "inside divisions double the text at every level. State "
                 "the inner ones as separate metrics.")}


def _scope_is_readable(scope: Any, query_id: str) -> Optional[Dict[str, Any]]:
    """The refusal a scope description earns, or nothing.

    A scope naming no set is not applied, but it is still read: a key
    misspelled and holding zero used to pass unnoticed, and the answer came
    back over the query's set with no sign that another one was asked for.
    """
    if scope is None:
        return None
    if not isinstance(scope, dict):
        return {"id": query_id,
                "error": f"scope must be an object, got {scope!r}.",
                "error_category": "invalid_argument"}
    verdict = _set_identifier(scope)
    if verdict.get("error"):
        return dict(verdict, id=query_id)
    return None


def _listed(value: Any) -> int:
    """How many entries a list holds, counting anything else as one.

    The count runs before the query is read, so it meets whatever the
    caller wrote. A number where a list belongs is refused later, by name;
    here it must not end the count with an interpreter error.
    """
    if value is None:
        return 0
    if isinstance(value, (list, tuple)):
        return len(value)
    return 1


def _metric_cost(metrics: Any, depth: int = 0) -> int:
    """How many expressions a list of metrics will build.

    A metric made of parts builds one per part, and a part may itself be
    made of parts. Counting the list alone let one allowed metric carry an
    expression of any size into a connection shared by every query.
    """
    if depth > MAX_NESTING or not isinstance(metrics, (list, tuple)):
        return 0 if depth <= MAX_NESTING else MAX_EXPRESSIONS_PER_CALL + 1
    total = 0
    for metric in metrics:
        if not isinstance(metric, dict):
            total += 1
            continue
        parts = metric.get("of")
        total += (_metric_cost(parts, depth + 1)
                  if isinstance(parts, (list, tuple)) else 1)
    return total


def _scope_names_a_set(scope: Any) -> bool:
    """Whether a scope description names a set at all.

    `{}` names none, and neither does every key left false or empty. Such
    a description is not a statement, and treating it as one cancelled the
    set the query had already named.
    """
    if scope is None:
        return False
    if not isinstance(scope, dict):
        # Not an object at all: let the check that refuses it see it.
        return True
    if scope.get("combine") is not None or scope.get("of") is not None:
        return True
    return any(bool(value) for value in scope.values())


def _filter_cost(query: Any, depth: int = 0) -> int:
    """How many Engine calls the filters of a query will cost.

    Every filter asks Engine about its field, and every value asks whether
    the field holds it. Counting only the values stated at the top let a
    metric carry three hundred of its own, or an element set hide them one
    level down, and pass a budget meant to bound exactly that.
    """
    if depth > MAX_NESTING:
        return MAX_EXPRESSIONS_PER_CALL + 1
    if isinstance(query, list):
        return sum(_filter_cost(item, depth + 1) for item in query)
    if not isinstance(query, dict):
        return 0
    total = 0
    for key, value in query.items():
        if key == "filters" and isinstance(value, list):
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                # The field itself is one call, then one per value.
                total += 1
                # A range is not one call: the working form of a date field
                # is chosen by measuring a reference against the candidate
                # forms, all in one batch but all of them expressions.
                if any(entry.get(key) is not None for key in
                       ("from", "to", "period", "greater_than", "less_than")):
                    total += RANGE_PROBE_COST
                for operator in ("values", "exclude", "add", "intersect"):
                    stated = entry.get(operator)
                    total += (len(stated) if isinstance(stated, (list, tuple))
                              else (1 if stated is not None else 0))
                for nested in ("matching", "not_matching"):
                    inner = entry.get(nested)
                    total += _filter_cost(inner, depth + 1)
                    # Reading from another field costs one more question.
                    # The same field is the one already asked about.
                    if (isinstance(inner, dict) and inner.get("of_field")
                            and bare_field_name(str(inner["of_field"]))
                            != bare_field_name(str(entry.get("field") or ""))):
                        total += 1
        elif isinstance(value, (list, dict)):
            total += _filter_cost(value, depth + 1)
    return total


def _as_number(value: Any) -> Optional[float]:
    """A stated bound as a number, or None when there is no bound."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

# Aggregations a metric may ask for, and the Qlik function behind each.
# `count` counts values of the field, `count_distinct` counts different
# ones — the pair a caller reaches for as "how many orders" against "how
# many customers".
# `TOTAL` before the modifier makes an aggregation ignore the grouping of
# the query — the whole of it, or all but the fields named after it. This
# is what "share of the total" is made of: the same sum, once per group
# and once across all of them, in one row.
AGGREGATIONS = {
    "sum": "Sum({modifier} {field})",
    "count": "Count({modifier} {field})",
    "count_distinct": "Count({modifier} DISTINCT {field})",
    "avg": "Avg({modifier} {field})",
    "min": "Min({modifier} {field})",
    "max": "Max({modifier} {field})",
    "median": "Median({modifier} {field})",
    "stdev": "Stdev({modifier} {field})",
    # A percentile needs the fraction to compute, so it carries `p`.
    "fractile": "Fractile({modifier} {field}, {p})",
}

# Aggregations that make sense over the result of an Aggr — that is, over
# one number per group rather than over rows. `count_distinct` is absent
# deliberately: it would count how many different per-group values there
# are, which is not "how many groups" and is rarely what anyone means.
OUTER_AGGREGATIONS = {
    "sum": "Sum({inner})",
    "count": "Count({inner})",
    "avg": "Avg({inner})",
    "min": "Min({inner})",
    "max": "Max({inner})",
    "median": "Median({inner})",
    "stdev": "Stdev({inner})",
    "fractile": "Fractile({inner}, {p})",
}

# Rows returned per query unless the caller says otherwise. Smaller than
# the hypercube default: a typed query is normally a ranking or a total,
# and a long tail of rows costs the reader more than it tells them.
DEFAULT_QUERY_LIMIT = 100

# Arithmetic between aggregations, as an operator over parts rather than
# as text to be parsed. Division guards itself: Qlik answers division by
# zero with a null, which reads as "no value" rather than as the mistake
# it is.
OPERATIONS = {
    "divide": " / ",
    "multiply": " * ",
    "add": " + ",
    "subtract": " - ",
}

# Where a described filter goes inside an expression the caller wrote. The
# same marker `engine_create_hypercube` uses, so the two tools ask for the
# same thing.
FILTER_MARKER = "{filter}"

# Queries one call may carry. The batch is sent to Engine before the first
# reply is read and every query holds a session object until the batch
# ends, so an unbounded list would hold the single shared socket, and the
# memory behind it, for as long as it took. Twenty-five is far above any
# real question and far below anything that hurts.
MAX_QUERIES_PER_CALL = 25

# Expressions one call may carry, counting every grouping field and every
# measure of every query. Capping the queries alone left the inner lists
# unbounded: one query holding ten thousand measures passed the check and
# then sent ten thousand expressions to Engine twice over, holding the
# single shared socket throughout.
MAX_EXPRESSIONS_PER_CALL = 200

# What a field name may look like when the server writes it into an
# expression. Qlik has no escape for `]` inside a bracketed name, so a
# name carrying one cannot be written safely — and `Amount]) + Sum([Amount`
# is a valid-looking name that turns one aggregation into two. Such a name
# is refused rather than quoted, and a genuine field with a bracket is
# reachable through a hand-written measure.
_UNSAFE_IN_FIELD_NAME = ("]", "[")


class EngineQueriesMixin:
    """Run typed queries, several at a time, over one socket."""

    def _plan_query(self, app_handle: int, app_id: str,
                    query: Dict[str, Any], position: int) -> Dict[str, Any]:
        """Turn one typed query into dimensions, measures and a modifier."""
        query_id = str(query.get("id") or f"q{position + 1}")

        for key in ("exclude_null_dimensions", "suppress_zero",
                    "include_raw_layout"):
            refusal = _yes_or_no(query.get(key), key, query_id)
            if refusal:
                return refusal

        group_by = query.get("group_by")
        if group_by is None:
            group_by = query.get("dimensions")
        if group_by is None:
            group_by = []
        if isinstance(group_by, str):
            group_by = [group_by]
        # A list of fields, or one field named plainly. An object was walked
        # by its keys, so the keys became the grouping and the values were
        # never read.
        if not isinstance(group_by, (list, tuple)):
            return {"id": query_id, "error": (
                f"group_by={group_by!r} is not a list of fields."),
                "error_category": "invalid_argument",
                "hint": 'A list of names: ["Region"], or one name: "Region".'}
        dimensions = []
        for item in group_by:
            field = item.get("field") if isinstance(item, dict) else item
            field = str(field or "").strip()
            if not field:
                return {"id": query_id, "error": (
                    f"Query {query_id!r} lists a grouping field with no name."),
                    "error_category": "invalid_argument"}
            if any(ch in bare_field_name(field) for ch in _UNSAFE_IN_FIELD_NAME):
                return {"id": query_id, "error": (
                    f"Grouping field {escape_qlik_field_name(field)} carries a "
                    f"bracket, which cannot be written into an expression "
                    f"unambiguously."),
                    "error_category": "invalid_argument"}
            # Bracketed once, here. The text of an expression is the key by
            # which Engine's verdict finds its way back to the query, and it
            # is built in two places independently — the batch collects it
            # for checking, the check builds it again. Wrap in one of them
            # and the keys drift apart, silently.
            dimensions.append({"field": escape_qlik_field_name(field)})

        filters = query.get("filters") or []
        # One modifier per distinct set of filters, built once and reused.
        # A KPI asks for two — the slice and everything — and building them
        # per measure would send the same values to Engine twice.
        slices = {}

        scope = query.get("scope")

        def slice_for(wanted, own_scope=None):
            chosen = own_scope if _scope_names_a_set(own_scope) else scope
            signature = json.dumps([wanted, chosen], sort_keys=True,
                                   ensure_ascii=False, default=str)
            if signature not in slices:
                slices[signature] = self.build_filters(
                    app_handle, app_id, wanted, scope=chosen)
            return slices[signature]

        built = slice_for(filters)
        if built.get("error"):
            reply = dict(built)
            reply["id"] = query_id
            return reply
        modifier = built.get("modifier", "")

        measures, error = self._build_measures(
            query, modifier, query_id, slice_for)
        if error:
            return error
        if not measures:
            return {"id": query_id, "error": (
                f"Query {query_id!r} asks for no measure."),
                "error_category": "invalid_argument",
                "hint": ('Add `metrics`, e.g. '
                         '[{"field": "Amount", "agg": "sum"}]. Aggregations: '
                         + ", ".join(sorted(AGGREGATIONS)) + ".")}

        return {
            "id": query_id,
            "dimensions": dimensions,
            "measures": measures,
            "modifier": modifier,
            "filters_applied": built.get("applied", []),
            # What the query counted over, when it is not the current
            # selections. Without it the reply cannot be told apart from
            # the same query with no scope at all.
            "scope": built.get("scope"),
            "limit": query.get("limit", DEFAULT_QUERY_LIMIT),
            "exclude_null_dimensions": query.get(
                "exclude_null_dimensions", False),
            "suppress_zero": query.get("suppress_zero", False),
            "include_raw_layout": query.get("include_raw_layout", False),
            "offset": query.get("offset", 0),
            "sort_by": query.get("sort_by"),
            "sort_order": query.get("sort_order", "desc"),
        }

    @staticmethod
    def _field_for_expression(name, what, query_id):
        """One field name, ready to be written into an expression."""
        field = bare_field_name(str(name or ""))
        if not field:
            return "", {"id": query_id, "error": f"{what} names no field.",
                        "error_category": "invalid_argument"}
        if any(ch in field for ch in _UNSAFE_IN_FIELD_NAME):
            return "", {"id": query_id, "error": (
                f"{what} [{field}] carries a bracket, which cannot be "
                f"written into an expression unambiguously."),
                "error_category": "invalid_argument"}
        return escape_qlik_field_name(field), None

    @staticmethod
    def _check_fraction(fraction, query_id):
        """A percentile needs a fraction, and one Qlik can use.

        Measured: `Fractile([days], 1.5)` answers "-" rather than an
        error, which reads as a value. So the bound is checked here.
        """
        if fraction is None:
            return {"id": query_id,
                    "error": "agg='fractile' names no p.",
                    "error_category": "invalid_argument",
                    "hint": ('Add "p": 0.85 for the 85th percentile. Qlik '
                             'takes p between 0 and 1.')}
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            return {"id": query_id,
                    "error": f"p={fraction!r} is not a number.",
                    "error_category": "invalid_argument"}
        if not 0 <= fraction <= 1:
            return {"id": query_id,
                    "error": f"p={fraction} is not between 0 and 1.",
                    "error_category": "invalid_argument",
                    "hint": ("0 is the smallest value, 1 the largest, 0.5 the "
                             "median. Qlik answers a fraction outside that "
                             "with a dash rather than an error.")}
        return None

    @staticmethod
    def _total_prefix(metric, query_id):
        """`TOTAL`, and the fields it should still respect.

        `total: true` ignores the grouping entirely — the denominator of a
        share. `total_except: ["Region"]` ignores all of it but those
        fields, which is a share within a group.
        """
        refusal = _yes_or_no(metric.get("total"), "total", query_id)
        if refusal:
            return "", refusal
        total = metric.get("total")
        except_fields = metric.get("total_except")
        if except_fields is not None:
            if isinstance(except_fields, (str, list, tuple)):
                names = ([except_fields] if isinstance(except_fields, str)
                         else list(except_fields))
            else:
                return "", {"id": query_id, "error": (
                    f"total_except={except_fields!r} is neither a field name "
                    f"nor a list of them."),
                    "error_category": "invalid_argument"}
            if not names:
                return "", {"id": query_id, "error": (
                    "total_except names no field."),
                    "error_category": "invalid_argument",
                    "hint": 'Use "total": true to ignore the grouping wholly.'}
            written = []
            for name in names:
                one, failure = EngineQueriesMixin._field_for_expression(
                    name, "total_except", query_id)
                if failure:
                    return "", failure
                written.append(one)
            return "TOTAL <" + ", ".join(written) + "> ", None
        if total:
            return "TOTAL ", None
        return "", None

    @staticmethod
    def _write_operation(metric, modifier, query_id, slice_for=None,
                         inherited_scope=None):
        """One arithmetic expression over two or more aggregations.

        Stated as an operator and its parts, not as text: `Sum(A)/Count(B)`
        written by hand would have to be parsed to know where a filter
        goes, and parsing Qlik is what this server does not do. As
        structure, each part keeps its own filter and its own nesting, and
        the division guard wraps the whole thing.
        """
        operation = str(metric.get("op") or "").strip().lower()
        if operation not in OPERATIONS:
            return {}, {"id": query_id, "error": (
                f"op={metric.get('op')!r} is not an operation this server "
                f"writes."),
                "error_category": "invalid_argument",
                "allowed_values": sorted(OPERATIONS)}
        parts = metric.get("of")
        if not isinstance(parts, list) or len(parts) < 2:
            return {}, {"id": query_id, "error": (
                f"op={operation!r} needs at least two parts in `of`."),
                "error_category": "invalid_argument",
                "hint": ('`of` holds metrics: [{"field": "Amount", "agg": '
                         '"sum"}, {"field": "OrderId", "agg": '
                         '"count_distinct"}].')}

        written = []
        part_filters = []
        for position, part in enumerate(parts):
            if not isinstance(part, dict):
                return {}, {"id": query_id, "error": (
                    f"A part of {operation!r} must be an object, got "
                    f"{part!r}."),
                    "error_category": "invalid_argument"}
            own = modifier
            own_applied = None
            own_scope = None
            stated_scope = (part.get("scope")
                            if _scope_names_a_set(part.get("scope"))
                            else inherited_scope)
            # Only a scope this part stated itself replaces what it
            # inherited. An inherited one arrives already built into
            # `modifier`, together with the filters that came with it.
            verdict = _scope_is_readable(part.get("scope"), query_id)
            if verdict:
                return {}, verdict
            if (_scope_names_a_set(part.get("scope"))
                    and part.get("filters") is None):
                built = slice_for([], part.get("scope"))
                if built.get("error"):
                    failed = dict(built)
                    failed["id"] = query_id
                    return {}, failed
                own = built.get("modifier", "")
                own_applied = built.get("applied", [])
                own_scope = built.get("scope")
            if "filters" in part and part["filters"] is not None:
                if slice_for is None or not isinstance(part["filters"], list):
                    return {}, {"id": query_id, "error": (
                        f"Part filters={part.get('filters')!r} is not a "
                        f"list."),
                        "error_category": "invalid_argument"}
                built = slice_for(part["filters"], stated_scope)
                if built.get("error"):
                    failed = dict(built)
                    failed["id"] = query_id
                    return {}, failed
                own = built.get("modifier", "")
                own_applied = built.get("applied", [])
                own_scope = built.get("scope")
            one, failure = EngineQueriesMixin._write_metric(
                part, own, query_id, slice_for, inherited_scope=stated_scope)
            if failure:
                return {}, failure
            written.append(
                f"({one['expression']})"
                if (part.get("op") is not None or part.get("of") is not None)
                else one["expression"])
            if own_applied is not None or own_scope is not None:
                part_filters.append({
                    "position": position,
                    "modifier": own,
                    "filters_applied": own_applied or [],
                    "scope": own_scope,
                })
            # An operation inside an operation carries slices of its own,
            # and losing them here would take the control probes of a
            # nested period with them.
            for nested in one.get("part_filters") or []:
                part_filters.append(dict(
                    nested, position=position,
                    label_path=[position] + (nested.get("label_path") or
                                             [nested.get("position")])))

        # Measured before it is built: the guard against zero writes every
        # denominator a second time, so nesting divisions doubles the text
        # at each level. Checking the finished string means the megabytes
        # have already been allocated - and one call may hold several of
        # these.
        # `a / b`, and for division `If((b) = 0, Null(), a / b)`: the
        # separators between the parts, then every denominator a second
        # time inside the guard.
        planned = (sum(len(one) for one in written)
                   + len(OPERATIONS[operation]) * (len(written) - 1))
        if operation == "divide":
            planned += (sum(len(one) + len("() = 0") for one in written[1:])
                        + len(" or ") * max(0, len(written) - 2)
                        + len("If(, Null(), )"))
        if planned > MAX_EXPRESSION_CHARS:
            return {}, {"id": query_id, "error": (
                f"This measure would be about {planned} characters long, "
                f"over the {MAX_EXPRESSION_CHARS} this server sends."),
                "error_category": "limit_exceeded",
                "hint": ("A division writes its denominator twice, so "
                         "divisions inside divisions double the text at "
                         "every level. State the inner ones as separate "
                         "metrics.")}

        joined = OPERATIONS[operation].join(written)
        if operation == "divide":
            # Qlik answers division by zero with a null, which shows up as
            # a dash and reads as "no value" rather than as a denominator
            # that was zero. Said plainly instead.
            denominators = written[1:]
            guard = " or ".join(f"({one}) = 0" for one in denominators)
            expression = f"If({guard}, Null(), {joined})"
        else:
            expression = joined
        written_metric = {"expression": expression,
                          "label": str(metric.get("label") or operation)}
        if part_filters:
            written_metric["part_filters"] = part_filters
        return written_metric, None

    @staticmethod
    def _write_metric(metric, modifier, query_id, slice_for=None,
                      inherited_scope=None):
        """Turn one metric into a Qlik expression.

        Two shapes. Flat is an aggregation over rows. Nested is an
        aggregation over one number per group, where `per` names the group
        and `inner_agg` says how that number is made.

        The second is not a convenience - it answers a different question.
        Measured on the same four rows: `Median([days])` over rows returned
        6, `Median(Aggr(Sum([days]), [issue]))` over issues returned 10.
        Asking the first when the second was meant returns a number that
        looks entirely reasonable.

        The filter goes into the innermost aggregation. That is the only
        function in the expression that reads rows of the table; `Aggr` and
        whatever wraps it work over numbers that already exist.
        """
        # Arithmetic first: an operation is a metric made of metrics, and
        # each part is built by the same rules — including its own filter,
        # its own grouping, its own nesting.
        if metric.get("op") is not None or metric.get("of") is not None:
            return EngineQueriesMixin._write_operation(
                metric, modifier, query_id, slice_for,
                inherited_scope=(metric.get("scope") if metric.get("scope")
                                 is not None else inherited_scope))

        prefix = (modifier + " ") if modifier else ""
        aggregation = str(metric.get("agg") or "sum").strip().lower()
        per = metric.get("per")
        inner_agg = metric.get("inner_agg")
        fraction = metric.get("p")

        field, failure = EngineQueriesMixin._field_for_expression(
            metric.get("field"), "Metric", query_id)
        if failure:
            return {}, failure
        plain_field = bare_field_name(field)

        if per is None and inner_agg is None:
            if aggregation not in AGGREGATIONS:
                return {}, {"id": query_id, "error": (
                    f"Aggregation {aggregation!r} is not one this server "
                    f"writes."),
                    "error_category": "invalid_argument",
                    "allowed_values": sorted(AGGREGATIONS),
                    "hint": ("For a calculation this vocabulary cannot "
                             "state, write the expression in `measures`, or "
                             "use engine_create_hypercube.")}
            if aggregation == "fractile":
                refusal = EngineQueriesMixin._check_fraction(
                    fraction, query_id)
                if refusal:
                    return {}, refusal
            total, failure = EngineQueriesMixin._total_prefix(metric, query_id)
            if failure:
                return {}, failure
            expression = AGGREGATIONS[aggregation].format(
                modifier=(total + prefix).rstrip() if (total or prefix) else "",
                field=field, p=fraction).replace("(  ", "(").replace("( ", "(")
            return {"expression": expression,
                    "label": str(metric.get("label")
                                 or f"{aggregation}_{plain_field}")}, None

        if inner_agg is None:
            return {}, {"id": query_id, "error": (
                f"Metric groups by per={per!r} but names no inner_agg."),
                "error_category": "invalid_argument",
                "allowed_values": sorted(AGGREGATIONS),
                "hint": ('Add "inner_agg": "sum" - the value computed for '
                         'each group, before the outer aggregation runs over '
                         'those values.')}
        if per is None:
            return {}, {"id": query_id, "error": (
                f"Metric names inner_agg={inner_agg!r} but nothing to group "
                f"by."),
                "error_category": "invalid_argument",
                "hint": ('Add "per": "OrderId" - the field each inner value '
                         'is computed for. Without it, drop inner_agg for a '
                         'plain aggregation.')}

        inner_name = str(inner_agg).strip().lower()
        if inner_name not in AGGREGATIONS or inner_name == "fractile":
            return {}, {"id": query_id, "error": (
                f"inner_agg={inner_agg!r} is not one this server writes "
                f"inside Aggr."),
                "error_category": "invalid_argument",
                "allowed_values": sorted(
                    name for name in AGGREGATIONS if name != "fractile")}
        if aggregation not in OUTER_AGGREGATIONS:
            return {}, {"id": query_id, "error": (
                f"Aggregation {aggregation!r} is not one this server writes "
                f"over groups."),
                "error_category": "invalid_argument",
                "allowed_values": sorted(OUTER_AGGREGATIONS),
                "hint": ("count_distinct over groups counts different group "
                         "values, not groups; count counts the groups."
                         if aggregation == "count_distinct" else
                         "For anything else, write the expression in "
                         "`measures`.")}
        if aggregation == "fractile":
            refusal = EngineQueriesMixin._check_fraction(fraction, query_id)
            if refusal:
                return {}, refusal

        if isinstance(per, (str, list, tuple)):
            per_fields = [per] if isinstance(per, str) else list(per)
        else:
            return {}, {"id": query_id, "error": (
                f"per={per!r} is neither a field name nor a list of them."),
                "error_category": "invalid_argument"}
        if not per_fields:
            return {}, {"id": query_id, "error": "per names no field.",
                        "error_category": "invalid_argument"}
        written_per = []
        for name in per_fields:
            one, failure = EngineQueriesMixin._field_for_expression(
                name, "per", query_id)
            if failure:
                return {}, failure
            if one in written_per:
                return {}, {"id": query_id,
                            "error": f"per names {one} twice.",
                            "error_category": "invalid_argument"}
            written_per.append(one)

        inner = AGGREGATIONS[inner_name].format(
            modifier=prefix.rstrip() if prefix else "",
            field=field, p=fraction).replace("(  ", "(").replace("( ", "(")
        grouped = "Aggr(" + inner + ", " + ", ".join(written_per) + ")"
        # TOTAL belongs to the outer aggregation: the inner one is already
        # grouped by `per`, and it is the outer one that would otherwise
        # follow the grouping of the query.
        total, failure = EngineQueriesMixin._total_prefix(metric, query_id)
        if failure:
            return {}, failure
        expression = OUTER_AGGREGATIONS[aggregation].format(
            inner=(total + grouped) if total else grouped, p=fraction)
        return {"expression": expression,
                "label": str(metric.get("label")
                             or f"{aggregation}_{inner_name}_{plain_field}")}, None

    @staticmethod
    def _build_measures(query: Dict[str, Any], modifier: str, query_id: str,
                        slice_for=None
                        ) -> "tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]":
        """Write one Qlik expression per metric.

        A metric names a field and an aggregation; the filter is folded
        into every one of them, because a set modifier applies to the
        aggregation it sits in and to nothing else.
        """
        measures: List[Dict[str, Any]] = []

        for key, shape in (("metrics", 'a list of metrics: [{"field": '
                                       '"Amount", "agg": "sum"}]'),
                           ("measures", 'a list of expressions: '
                                        '["Sum([Amount])"]')):
            stated = query.get(key)
            if stated is not None and not isinstance(stated, (list, tuple)):
                return [], {"id": query_id, "error": (
                    f"{key}={stated!r} is not a list."),
                    "error_category": "invalid_argument",
                    "hint": f"Use {shape}."}
        stated_metrics = query.get("metrics")
        for metric in stated_metrics or []:
            if isinstance(metric, str):
                metric = {"field": metric, "agg": "sum"}
            if not isinstance(metric, dict):
                return [], {"id": query_id, "error": (
                    f"A metric must be an object, got {metric!r}."),
                    "error_category": "invalid_argument"}
            # A metric may narrow itself differently from the query, so a
            # KPI can hold its numerator and denominator side by side. No
            # key means the query's filters; an empty list means none at
            # all, and the two are different statements.
            own = modifier
            own_applied = None
            own_scope = None
            # A scope of its own applies even with no filters beside it:
            # "everything, ignoring selections" is a statement in itself.
            verdict = _scope_is_readable(metric.get("scope"), query_id)
            if verdict:
                return [], verdict
            if (_scope_names_a_set(metric.get("scope"))
                    and metric.get("filters") is None):
                built = slice_for([], metric.get("scope"))
                if built.get("error"):
                    failed = dict(built)
                    failed["id"] = query_id
                    return [], failed
                own = built.get("modifier", "")
                own_applied = built.get("applied", [])
                own_scope = built.get("scope")
            if "filters" in metric and metric["filters"] is not None:
                if not isinstance(metric["filters"], list):
                    return [], {"id": query_id, "error": (
                        f"Metric filters={metric['filters']!r} is not a "
                        f"list."),
                        "error_category": "invalid_argument",
                        "hint": ('Use [] for no filter at all, or a list of '
                                 'filter objects.')}
                if slice_for is None:
                    return [], {"id": query_id, "error": (
                        "Per-metric filters are not available here."),
                        "error_category": "invalid_argument"}
                built = slice_for(metric["filters"], metric.get("scope"))
                if built.get("error"):
                    failed = dict(built)
                    failed["id"] = query_id
                    return [], failed
                own = built.get("modifier", "")
                own_applied = built.get("applied", [])
                own_scope = built.get("scope")
            written, failure = EngineQueriesMixin._write_metric(
                metric, own, query_id, slice_for,
                inherited_scope=metric.get("scope"))
            if failure:
                return [], failure
            if own_applied is not None:
                written["filters_applied"] = own_applied
                written["modifier"] = own
            if own_scope:
                written["scope"] = own_scope
            oversized = _too_long(written["expression"], query_id)
            if oversized:
                return [], oversized
            measures.append(written)

        # A caller that needs something this vocabulary cannot say writes
        # the expression itself. A filter cannot be folded into an arbitrary
        # expression on its behalf — a set modifier narrows the aggregation
        # it sits in, and `Sum(A)/Count(B)` has two of them — so the
        # expression marks where it goes, exactly as in
        # engine_create_hypercube.
        for measure in query.get("measures") or []:
            if isinstance(measure, str):
                measure = {"expression": measure}
            expression = str(measure.get("expression") or "").strip()
            if not expression:
                return [], {"id": query_id, "error": (
                    f"Measure {measure!r} carries no expression."),
                    "error_category": "invalid_argument"}
            own = modifier
            own_applied = None
            own_scope = None
            verdict = _scope_is_readable(measure.get("scope"), query_id)
            if verdict:
                return [], verdict
            if (_scope_names_a_set(measure.get("scope"))
                    and measure.get("filters") is None):
                built = slice_for([], measure.get("scope"))
                if built.get("error"):
                    failed = dict(built)
                    failed["id"] = query_id
                    return [], failed
                own = built.get("modifier", "")
                own_applied = built.get("applied", [])
                own_scope = built.get("scope")
            if "filters" in measure and measure["filters"] is not None:
                if slice_for is None or not isinstance(measure["filters"], list):
                    return [], {"id": query_id, "error": (
                        f"Measure filters={measure.get('filters')!r} is not a "
                        f"list."),
                        "error_category": "invalid_argument"}
                built = slice_for(measure["filters"], measure.get("scope"))
                if built.get("error"):
                    failed = dict(built)
                    failed["id"] = query_id
                    return [], failed
                own = built.get("modifier", "")
                own_applied = built.get("applied", [])
                own_scope = built.get("scope")
            # `own`, never `modifier`: writing back into the parameter made
            # the first measure with its own filter the base for the next
            # one, so a measure that stated no filter inherited its
            # neighbour's instead of the query's — silently, and with a
            # plausible number to show for it.
            # A measure that stated `filters: []` said "no filter" — the
            # marker then has nothing to hold and comes out. Only a marker
            # with no filter stated anywhere is a mistake worth refusing.
            if own_applied is not None and not own:
                expression = expression.replace(FILTER_MARKER, "").replace(
                    "(  ", "(").replace("( ", "(")
            elif not own and FILTER_MARKER in expression:
                return [], {"id": query_id, "error": (
                    f"Measure {expression!r} marks a place for a filter, but "
                    f"the query states none."),
                    "error_category": "invalid_argument",
                    "hint": ("Add `filters`, or drop the marker — Qlik has no "
                             "meaning for it and would read it as text.")}
            if own:
                if FILTER_MARKER not in expression:
                    return [], {"id": query_id, "error": (
                        f"Measure {expression!r} is written by hand and this "
                        f"query has filters, but the expression does not say "
                        f"where the filter goes."),
                        "error_category": "invalid_argument",
                        "next_actions": [
                            "write the marker inside the aggregation the "
                            "filter narrows: \"Sum({filter} Amount)\"",
                            "or state the measure as a metric: "
                            '{"field": "Amount", "agg": "sum"}',
                        ],
                        "hint": (
                            "Without this the aggregation would run over "
                            "every row while the reply said the period had "
                            "been applied."
                        )}
                expression = expression.replace(FILTER_MARKER, own)
            written = {"expression": expression,
                       "label": str(measure.get("label") or expression)}
            if own_applied is not None:
                written["filters_applied"] = own_applied
                written["modifier"] = own
            if own_scope:
                written["scope"] = own_scope
            oversized = _too_long(written["expression"], query_id)
            if oversized:
                return [], oversized
            measures.append(written)
        return measures, None

    def _control_probes(self, plan: Dict[str, Any]) -> List[Dict[str, str]]:
        """Expressions that show whether a period filter really applied.

        For each period filter: the earliest and latest value of that field
        inside the filtered set. A value outside the requested period means
        Qlik ignored the condition — the one failure that otherwise returns
        a plausible number and no sign of trouble.
        """
        # Every filter stated anywhere in the query, not only the one at
        # the top: a period named inside a metric narrows that metric, and
        # a period that fails to apply is exactly what these probes exist
        # to catch. The measure's own modifier is used for its own probe.
        # A combination of sets records what each of its sets narrowed, one
        # level down: a period stated inside one of them can fail to apply
        # like any other, and the reply promises to say so.
        def _narrowings(records, modifier):
            for record in records or []:
                if isinstance(record, dict) and "filters_applied" in record:
                    for inner in _narrowings(record["filters_applied"],
                                             record.get("modifier")
                                             or modifier):
                        yield inner
                elif isinstance(record, dict):
                    yield (modifier, record)

        stated = list(_narrowings(plan.get("filters_applied"),
                                  plan.get("modifier", "")))
        for measure in plan.get("measures", []):
            stated.extend(_narrowings(measure.get("filters_applied"),
                                      measure.get("modifier", "")))
            # A part of an arithmetic metric carries its own filters, and a
            # period stated there can fail to apply like any other.
            for part in measure.get("part_filters") or []:
                stated.extend(_narrowings(part.get("filters_applied"),
                                          part.get("modifier", "")))

        probes = []
        seen = set()
        for modifier_for_probe, applied in stated:
            signature = (modifier_for_probe, applied.get("field"),
                         str(applied.get("from")), str(applied.get("to")))
            if signature in seen:
                continue
            seen.add(signature)
            # A period and a numeric range are both bounded filters, and
            # both can silently fail to apply. Values outside the bounds
            # in the result say so; a value list cannot fail this way,
            # since each value was checked against the field.
            if "serial_from" not in applied and "from" not in applied:
                continue
            if applied.get("from") is None and applied.get("to") is None:
                continue
            field = escape_qlik_field_name(applied["field"])
            inner = (f"{modifier_for_probe} {field}"
                     if modifier_for_probe else field)
            # A period compares against day numbers; a range against the
            # values themselves. Either way the question is the same: does
            # the result hold anything outside what was asked for.
            is_period = "serial_from" in applied
            low_bound = (applied["serial_from"] if is_period
                         else _as_number(applied.get("from")))
            high_bound = (applied["serial_to_exclusive"] if is_period
                          else _as_number(applied.get("to")))
            probes.append({
                "field": applied["field"],
                "expected_from": applied["from"],
                "expected_to": applied["to"],
                "serial_from": low_bound,
                "serial_to_exclusive": high_bound,
                "inclusive_upper": not is_period and not applied.get("to_excluded"),
                "exclusive_lower": bool(applied.get("from_excluded")),
                "earliest": f"=Text(Min({inner}))",
                "latest": f"=Text(Max({inner}))",
                "earliest_number": f"=Num(Min({inner}))",
                "latest_number": f"=Num(Max({inner}))",
            })
        return probes

    @staticmethod
    def _read_controls(probe: Dict[str, str],
                       values: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Turn four evaluated probes into one statement about the period."""
        earliest, latest, earliest_num, latest_num = values
        check: Dict[str, Any] = {
            "field": probe["field"],
            "requested_from": probe["expected_from"],
            "requested_to": probe["expected_to"],
            "earliest_in_result": earliest.get("text"),
            "latest_in_result": latest.get("text"),
        }
        low = earliest_num.get("number")
        high = latest_num.get("number")
        if not isinstance(low, (int, float)) or not isinstance(high, (int, float)):
            # Two very different answers arrive in the same shape. Qlik
            # complaining is a check that failed to run; an empty
            # intersection is a check with nothing to look at - January and
            # a region with no January sales. Engine answers Min/Max over
            # an empty set with its "NaN" sentinel.
            complaint = ""
            for value in (earliest_num, latest_num, earliest, latest):
                text = str((value or {}).get("text") or "")
                if text.startswith("Error"):
                    complaint = text.split("Error:", 1)[-1].strip() or text
                    break
            check["filter_applied"] = None
            check["note"] = (
                f"The period could not be checked: Qlik answered "
                f"{complaint}"
                if complaint else
                "The filters together select no rows, so there is nothing "
                "to check the period against."
            )
            return check
        lower = probe.get("serial_from")
        upper = probe.get("serial_to_exclusive")
        outside = False
        if lower is not None:
            outside = outside or (low <= lower if probe.get("exclusive_lower")
                                  else low < lower)
        if upper is not None:
            # A period's upper bound is the next day and excludes it; a
            # numeric range includes the bound the caller named.
            outside = outside or (high > upper if probe.get("inclusive_upper")
                                  else high >= upper)
        check["filter_applied"] = not outside
        if outside:
            check["note"] = (
                f"The result holds values of {probe['field']} outside "
                f"{probe['expected_from']}..{probe['expected_to']}, so the "
                f"period did not narrow it. Treat the numbers as unfiltered."
            )
        return check

    def run_queries(self, app_handle: int, app_id: str,
                    queries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Run every query in the list, sharing three round-trips.

        Independent queries do not need to wait for each other: the Engine
        JSON API matches replies to requests by id, so all the session
        objects are created in one batch, all the layouts read in the next,
        and all the objects released in the third. A batch of five costs
        three round-trips rather than fifteen.

        A query that fails takes only itself down. Its entry carries the
        reason; the rest of the batch still answers.
        """
        started = time.monotonic()
        expressions = sum(
            _listed(q.get("group_by") or q.get("dimensions"))
            + _metric_cost(q.get("metrics")) + _listed(q.get("measures"))
            + _filter_cost(q)
            for q in queries if isinstance(q, dict))
        if expressions > MAX_EXPRESSIONS_PER_CALL:
            return {
                "error": (
                    f"{expressions} grouping fields and measures in one call; "
                    f"the cap is {MAX_EXPRESSIONS_PER_CALL}."
                ),
                "error_category": "limit_exceeded",
                "hint": ("Every one of them is checked by Engine before the "
                         "batch runs. Ask fewer things at a time."),
            }
        if len(queries) > MAX_QUERIES_PER_CALL:
            # Refused before anything is planned or opened: the whole batch
            # goes to Engine before the first reply is read, and every
            # query holds a session object until it ends.
            return {
                "error": (
                    f"{len(queries)} queries in one call; the cap is "
                    f"{MAX_QUERIES_PER_CALL}."
                ),
                "error_category": "limit_exceeded",
                "hint": (
                    f"Send up to {MAX_QUERIES_PER_CALL} at a time. They run "
                    f"together, so a batch that size already costs the same "
                    f"three round-trips as one query."
                ),
            }
        plans: List[Dict[str, Any]] = []
        for position, query in enumerate(queries):
            if not isinstance(query, dict):
                plans.append({"id": f"q{position + 1}", "error": (
                    f"A query must be an object, got {query!r}."),
                    "error_category": "invalid_argument"})
                continue
            try:
                plans.append(self._plan_query(app_handle, app_id, query, position))
            except Exception as exc:
                logger.exception("Planning query %d failed", position)
                plans.append({"id": str(query.get("id") or f"q{position + 1}"),
                              "error": str(exc), "error_category": "unexpected"})

        runnable = [plan for plan in plans if not plan.get("error")]

        # Engine is asked about every expression in the batch at once —
        # three pipelined calls whatever the batch size — and then each
        # query is judged on its own expressions. Judging the batch as a
        # whole would lose which query owns which mistake: one bad name
        # would either refuse its neighbours or, with two mistakes of
        # different kinds, let the second through unchecked.
        if runnable:
            inspection = self.inspect_expressions(app_handle, [
                text for plan in runnable
                for text in ([str(d.get("field") or "") for d in plan["dimensions"]]
                             + [str(m.get("expression") or "")
                                for m in plan["measures"]])
            ])
            for plan in runnable:
                verdict = self._validate_cube_inputs(
                    app_handle, plan["dimensions"], plan["measures"],
                    inspection=inspection)
                if verdict.get("error"):
                    plan.update(verdict)
                elif verdict.get("warnings"):
                    plan.setdefault("warnings", []).extend(verdict["warnings"])
        runnable = [plan for plan in plans if not plan.get("error")]

        prepared = []
        for plan in runnable:
            try:
                shaped = self._shape_cube(plan)
            except Exception as exc:
                # One malformed argument must not take the batch down —
                # that is the whole promise of running them together.
                logger.debug("Shaping query %s failed: %s", plan.get("id"), exc)
                plan.update({"error": str(exc),
                             "error_category": "invalid_argument"})
                continue
            if shaped.get("error"):
                plan.update(shaped)
                continue
            prepared.append((plan, shaped))
        if not prepared:
            return {"results": [self._query_reply(p, None, None) for p in plans],
                    "seconds": round(time.monotonic() - started, 3)}

        # From here on Engine holds objects for this batch, and they are
        # released on every path out — including a transport failure part
        # way through. A leak pins each result set in Engine memory for the
        # rest of a session this server keeps open by design.
        created_ids: List[str] = [shaped["object"]["qInfo"]["qId"]
                                  for _, shaped in prepared]
        try:
            return self._run_prepared(app_handle, plans, prepared, started)
        finally:
            # getattr: a killed socket has nothing left to talk to, and an
            # instance built without __init__ has no attribute to ask.
            if created_ids and getattr(self, "ws", True) is not None:
                try:
                    self.send_requests_pipelined(
                        [{"method": "DestroySessionObject", "params": [qid],
                          "handle": app_handle} for qid in created_ids],
                        raise_on_error=False,
                        timeout=self.ws_operation_timeout,
                    )
                except Exception as exc:
                    # Never turn a finished batch into a failure because the
                    # cleanup did not go through.
                    logger.warning("Releasing batch session objects failed: %s",
                                   exc)

    def _run_prepared(self, app_handle: int, plans: List[Dict[str, Any]],
                      prepared: List[tuple], started: float) -> Dict[str, Any]:
        """Create, read and answer, for the queries that survived planning."""
        # Batch one: create every session object, and evaluate every
        # control probe alongside them.
        creates = [
            {"method": "CreateSessionObject", "params": [shaped["object"]],
             "handle": app_handle}
            for _, shaped in prepared
        ]
        probe_map: List[tuple] = []
        probe_requests = []
        for plan, _ in prepared:
            for probe in self._control_probes(plan):
                start = len(probe_requests)
                for key in ("earliest", "latest", "earliest_number",
                            "latest_number"):
                    probe_requests.append(
                        {"method": "EvaluateEx", "params": [probe[key]],
                         "handle": app_handle})
                probe_map.append((plan, probe, start))

        outcomes = self.send_requests_pipelined(
            creates + probe_requests, raise_on_error=False,
            timeout=self.ws_operation_timeout)
        create_outcomes = outcomes[:len(creates)]
        probe_outcomes = outcomes[len(creates):]

        for plan, probe, start in probe_map:
            values = []
            for outcome in probe_outcomes[start:start + 4]:
                if isinstance(outcome, Exception):
                    values.append({"text": None, "number": None})
                    continue
                value = (outcome or {}).get("qValue") or {}
                values.append({"text": value.get("qText"),
                               "number": value.get("qNumber")})
            plan.setdefault("period_check", []).append(
                self._read_controls(probe, values))

        handles = []
        for (plan, shaped), outcome in zip(prepared, create_outcomes):
            if isinstance(outcome, Exception):
                plan["error"] = str(outcome)
                plan["error_category"] = "engine_api_error"
                continue
            handle = ((outcome or {}).get("qReturn") or {}).get("qHandle")
            if handle is None:
                plan["error"] = "Engine created no object for this query"
                plan["error_category"] = "engine_api_error"
                continue
            plan["handle"] = handle
            handles.append((plan, shaped, handle))

        # Batch two: read every layout.
        layouts = self.send_requests_pipelined(
            [{"method": "GetLayout", "params": [], "handle": handle}
             for _, _, handle in handles],
            raise_on_error=False, timeout=self.ws_operation_timeout,
        ) if handles else []

        for (plan, shaped, handle), layout in zip(handles, layouts):
            if isinstance(layout, Exception):
                plan["error"] = str(layout)
                plan["error_category"] = "engine_api_error"
                continue
            cube = ((layout or {}).get("qLayout") or {}).get("qHyperCube")
            if not cube:
                plan["error"] = "Engine returned no hypercube for this query"
                plan["error_category"] = "engine_api_error"
                continue
            plan["cube"] = cube
            plan["shaped"] = shaped

        # Engine may hand back a shorter first page than was asked for, and
        # the caller has no way to tell a short page from a short result.
        # The hypercube path reads the rest; this one used to stop there and
        # answer with fewer rows for the same question.
        short = []
        for plan in plans:
            cube, shaped = plan.get("cube"), plan.get("shaped")
            if not cube or not shaped:
                continue
            got = sum(len(page.get("qMatrix") or [])
                      for page in cube.get("qDataPages") or [])
            total = (cube.get("qSize") or {}).get("qcy", 0)
            wanted = min(shaped["limit"], max(0, total - shaped["offset"]))
            if got < wanted:
                # A list, and the same object the loop below marks: the
                # reason one query came up short is that query's own.
                short.append([plan, cube, shaped, got, wanted])
        # One reply may itself be short, so the reading-on repeats until
        # the page is whole or Engine stops adding rows. Whatever is still
        # missing is said out loud rather than answered as a smaller
        # result.
        pending = list(short)
        for _ in range(MAX_PAGE_READS):
            pending = [item for item in pending if item[3] < item[4]]
            if not pending:
                break
            requests = [{
                "method": "GetHyperCubeData",
                "handle": plan["handle"],
                "params": ["/qHyperCubeDef", [{
                    "qTop": shaped["offset"] + got, "qLeft": 0,
                    "qHeight": wanted - got,
                    "qWidth": len(shaped["column_names"])}]],
            } for plan, cube, shaped, got, wanted in pending]
            try:
                extra = self.send_requests_pipelined(requests,
                                                     raise_on_error=False)
            except Exception:
                extra = [None] * len(requests)
            progressed = []
            for item, reply in zip(pending, extra):
                if isinstance(reply, Exception) or not reply:
                    continue
                pages = reply.get("qDataPages") or []
                added = sum(len(page.get("qMatrix") or []) for page in pages)
                if not added:
                    continue
                item[1]["qDataPages"] = (item[1].get("qDataPages") or []) + pages
                item[3] += added
                progressed.append(id(item))
            # Per query, not per batch: one may have run out of rows while
            # another is still being served.
            for item in pending:
                if id(item) not in progressed:
                    item.append("stopped")
            pending = [item for item in pending if len(item) == 5]
            if not pending:
                break
        for item in short:
            plan, cube, shaped, got, wanted = item[:5]
            if got >= wanted:
                continue
            plan.setdefault("warnings", []).append(
                f"{got} of the {wanted} rows this page asked for came "
                f"back; "
                + ("Engine stopped sending."
                   if len(item) > 5 else
                   f"reading on stopped after {MAX_PAGE_READS} rounds.")
                + f" Ask again for the rest with "
                  f"offset={shaped['offset'] + got}."
            )

        results = [
            self._query_reply(plan, plan.get("cube"), plan.get("shaped"))
            for plan in plans
        ]
        return {
            "results": results,
            "queries_run": len([r for r in results if not r.get("error")]),
            "queries_failed": len([r for r in results if r.get("error")]),
            "seconds": round(time.monotonic() - started, 3),
        }

    def _shape_cube(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve limits and sorting, then build the object definition."""
        dimensions = [dict(d, sort_by={"qSortByNumeric": 0, "qSortByAscii": 1,
                                       "qSortByExpression": 0, "qExpression": ""})
                      for d in plan["dimensions"]]
        measures = [dict(m, sort_by={"qSortByNumeric": -1})
                    for m in plan["measures"]]
        n_dims = len(dimensions)
        n_cols = n_dims + len(measures)
        column_names = self._column_names(dimensions, measures)

        # A limit the caller did not state defaults; one it stated wrongly
        # is refused. Reading `limit=0` as "give me a hundred rows" answers
        # a question nobody asked.
        limit = plan.get("limit")
        if limit is None:
            limit = DEFAULT_QUERY_LIMIT
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            return {"error": f"limit={limit!r} is not a positive row count.",
                    "error_category": "invalid_limit",
                    "hint": (f"Pass an integer between 1 and "
                             f"{self.HARD_MAX_ROWS}, or omit it for "
                             f"{DEFAULT_QUERY_LIMIT}.")}
        if limit > self.HARD_MAX_ROWS:
            return {"error": (
                f"limit={limit} is above the ceiling of "
                f"{self.HARD_MAX_ROWS} rows."),
                "error_category": "limit_exceeded",
                "hint": (f"Ask for at most {self.HARD_MAX_ROWS}, or narrow "
                         f"the query with filters.")}
        if n_cols and n_cols * limit > self.HARD_MAX_CELLS:
            # Refused rather than reduced: a limit quietly cut to fit
            # returns fewer rows than were asked for, and nothing in the
            # reply says which of the two happened.
            fits = max(1, self.HARD_MAX_CELLS // n_cols)
            return {"error": (
                f"{n_cols} columns times {limit} rows is "
                f"{n_cols * limit} cells, above Qlik's ceiling of "
                f"{self.HARD_MAX_CELLS} per page."),
                "error_category": "cell_cap_exceeded",
                "hint": (f"At {n_cols} columns the most that fits is "
                         f"limit={fits}. Fewer measures raise it.")}

        sort_index = None
        direction = None
        if plan.get("sort_by") is not None and n_cols:
            direction = self._normalize_sort_order(plan.get("sort_order"))
            if direction is None:
                return {"error": (
                    f"sort_order={plan.get('sort_order')!r} is not a sort "
                    f"direction."),
                    "error_category": "invalid_sort",
                    "allowed_values": ["desc", "asc"]}
            sort_index = self._resolve_sort_column(
                plan["sort_by"], dimensions, measures)
            if sort_index is None:
                return {"error": (
                    f"sort_by={plan['sort_by']!r} names no column of this "
                    f"query."),
                    "error_category": "invalid_sort",
                    "available_columns": column_names}

        order = list(range(n_cols))
        if sort_index is not None and direction is not None:
            order = [sort_index] + [i for i in range(n_cols) if i != sort_index]
            if sort_index >= n_dims:
                measures[sort_index - n_dims]["sort_by"] = {
                    "qSortByNumeric": direction}
            else:
                dimensions[sort_index]["sort_by"] = {
                    "qSortByNumeric": direction, "qSortByAscii": direction,
                    "qSortByExpression": 0, "qExpression": ""}

        offset = plan.get("offset")
        if offset is None:
            offset = 0
        if (not isinstance(offset, int) or isinstance(offset, bool)
                or offset < 0):
            return {"error": f"offset={offset!r} is not a row number.",
                    "error_category": "invalid_argument",
                    "hint": "Pass 0 or a positive integer, or omit it."}
        return {
            "object": {
                "qInfo": {"qId": f"query-{uuid.uuid4().hex[:12]}",
                          "qType": "HyperCube"},
                "qHyperCubeDef": self._hypercube_def(
                    dimensions, measures, offset, limit, order,
                    suppress_zero=bool(plan.get("suppress_zero")),
                    exclude_null_dimensions=bool(
                        plan.get("exclude_null_dimensions"))),
            },
            "column_names": column_names,
            "n_dims": n_dims,
            "limit": limit,
            "offset": offset,
            "sorted_by": column_names[sort_index] if sort_index is not None else None,
            "sort_order": (("desc" if direction == -1 else "asc")
                           if sort_index is not None else None),
        }

    def _query_reply(self, plan: Dict[str, Any], cube: Optional[Dict[str, Any]],
                     shaped: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The answer to one query in the batch."""
        if plan.get("error"):
            reply = {k: v for k, v in plan.items()
                     if k in ("id", "error", "error_category", "hint",
                              "did_you_mean", "unknown_fields",
                              "unknown_values", "allowed_values",
                              "accepted_forms", "next_actions",
                              "invalid_expressions", "available_columns")}
            return reply
        if cube is None or shaped is None:
            return {"id": plan.get("id"),
                    "error": "This query did not run",
                    "error_category": "engine_api_error"}

        column_names = shaped["column_names"]
        n_dims = shaped["n_dims"]
        temporal = self._temporal_columns(cube)
        rows = self._matrix_to_rows(cube.get("qDataPages") or [],
                                    column_names, temporal)
        total_rows = (cube.get("qSize") or {}).get("qcy", 0)

        warnings: List[str] = []
        for column in self._measure_columns_are_empty(rows, n_dims, column_names):
            warnings.append(
                f"Every value of {column!r} came back 0 or '-'. Qlik returns "
                f"that for an aggregation over no rows; check the filters."
            )
        if not rows:
            # Two different answers look alike here, and saying only one of
            # them sends the caller after data that is there.
            warnings.append(
                "No rows came back. With suppress_zero=true that is also "
                "what a measure evaluating to 0 everywhere looks like; "
                "otherwise the grouping fields have no values under these "
                "filters. Ask again without suppress_zero to tell them "
                "apart."
                if plan.get("suppress_zero") else
                "No rows matched. The grouping fields have no values under "
                "these filters."
            )
        for check in plan.get("period_check", []):
            if check.get("filter_applied") is False:
                warnings.append(check["note"])
        # What the checks said before the query ran — which fields a set
        # modifier really filters on, for instance — belongs in the same
        # place as what the result said afterwards.
        warnings.extend(plan.get("warnings") or [])

        reply: Dict[str, Any] = {
            "id": plan["id"],
            "columns": column_names,
            "rows": rows,
            "returned_rows": len(rows),
            "total_rows": total_rows,
            "grand_total": [
                cell.get("qText")
                if (cell.get("qNum") in (None, "NaN")
                    or (n_dims + index) in temporal)
                else cell.get("qNum")
                for index, cell in enumerate(cube.get("qGrandTotalRow") or [])
            ],
            "sorted_by": shaped["sorted_by"],
            "sort_order": shaped["sort_order"],
        }
        if plan.get("filters_applied"):
            reply["filters_applied"] = plan["filters_applied"]
        if plan.get("scope"):
            reply["scope"] = plan["scope"]
        # Asked for, so answered: the untouched Qlik layout, exactly as
        # engine_create_hypercube hands it over.
        if plan.get("include_raw_layout") and cube is not None:
            reply["hypercube_data"] = cube
        # Where measures were narrowed differently from each other, say so
        # per measure: the number alone does not show which slice it came
        # from, and a KPI holding both is the point of the feature.
        per_measure = []
        for measure in plan.get("measures", []):
            if "filters_applied" in measure or measure.get("scope"):
                entry = {"label": measure.get("label"),
                         "filters_applied": measure.get("filters_applied") or []}
                # The set a measure is counted over is as much a part of
                # its slice as the filters are: counted over a bookmark
                # with no filters, it would otherwise read "no slice".
                if measure.get("scope"):
                    entry["scope"] = measure["scope"]
                per_measure.append(entry)
            # A measure made of parts narrows each of them on its own, and
            # the number alone does not show which part used which slice.
            for part in measure.get("part_filters") or []:
                path = part.get("label_path") or [part.get("position", 0)]
                where = "".join(f"[{step + 1}]" for step in path)
                entry = {"label": f"{measure.get('label')} {where}",
                         "filters_applied": part.get("filters_applied") or []}
                if part.get("scope"):
                    entry["scope"] = part["scope"]
                per_measure.append(entry)
        if per_measure:
            reply["measure_filters"] = per_measure
        if plan.get("period_check"):
            reply["period_check"] = plan["period_check"]
        if shaped["offset"] + len(rows) < total_rows:
            reply["has_more"] = True
            reply["next_offset"] = shaped["offset"] + len(rows)
            if shaped["sorted_by"] is None:
                warnings.append(
                    f"{total_rows} groups exist and {len(rows)} came back, "
                    f"in no particular order. Set `sort_by` to a measure "
                    f"label to get the largest ones."
                )
        if warnings:
            reply["warnings"] = warnings
        return reply
