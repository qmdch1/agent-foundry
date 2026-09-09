"""Foundry comparison template v1.0.0; missing values never satisfy filters."""

from decimal import Decimal, InvalidOperation

TEMPLATE_VERSION = "1.0.0"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Expected finite numeric value")
    if len(str(value)) > 100:
        raise ValueError("Numeric value is too long")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Expected finite numeric value") from exc
    if not result.is_finite() or abs(result.adjusted()) > 100:
        raise ValueError("Numeric value exceeds supported bounds")
    return result


def compare(records, filters=None, sort_by=None, descending=False, limit=100):
    """Filter exact values or numeric ranges; optionally sort a numeric field."""
    if (
        not isinstance(records, list)
        or len(records) > 10000
        or any(not isinstance(item, dict) for item in records)
    ):
        raise ValueError("Expected at most 10000 objects")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("Limit must be 1..1000")
    if not isinstance(descending, bool):
        raise ValueError("Descending must be boolean")
    if sort_by is not None and (not isinstance(sort_by, str) or not sort_by):
        raise ValueError("Expected sort field name")
    filters = [] if filters is None else filters
    if not isinstance(filters, list) or len(filters) > 20:
        raise ValueError("Expected at most 20 filters")
    for condition in filters:
        if not isinstance(condition, dict) or set(condition) != {"field", "op", "value"}:
            raise ValueError("Each filter requires field, op and value")
        if not isinstance(condition["field"], str) or not condition["field"]:
            raise ValueError("Expected filter field name")
        if condition["op"] not in {"eq", "gte", "lte", "gt", "lt"}:
            raise ValueError("Unsupported comparison operation")
        if condition["op"] != "eq":
            _number(condition["value"])

    def matches(record):
        for condition in filters:
            value = record.get(condition["field"])
            if value is None:
                return False
            expected, op = condition["value"], condition["op"]
            if op == "eq":
                if type(value) is not type(expected) or value != expected:
                    return False
            else:
                actual, target = _number(value), _number(expected)
                if not {
                    "gte": actual >= target,
                    "lte": actual <= target,
                    "gt": actual > target,
                    "lt": actual < target,
                }[op]:
                    return False
        return True

    selected = [item for item in records if matches(item)]
    if sort_by:
        known = [item for item in selected if item.get(sort_by) is not None]
        missing = [item for item in selected if item.get(sort_by) is None]
        selected = sorted(known, key=lambda item: _number(item[sort_by]), reverse=descending) + missing
    return {"items": selected[:limit], "matched": len(selected), "total": len(records)}
