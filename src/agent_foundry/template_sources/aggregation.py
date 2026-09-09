"""Foundry aggregation template v1.0.0; decimal results are JSON strings."""

from decimal import Decimal, InvalidOperation, localcontext

TEMPLATE_VERSION = "1.0.0"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Expected finite decimal number")
    text = str(value)
    if len(text) > 100:
        raise ValueError("Number is too long")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal number") from exc
    if not number.is_finite() or abs(number.adjusted()) > 100:
        raise ValueError("Number exceeds supported bounds")
    return number


def aggregate(records, field, group_by=None):
    """Aggregate up to 10,000 records; reject missing or nonnumeric values."""
    if not isinstance(records, list) or not records or len(records) > 10000:
        raise ValueError("Expected 1..10000 records")
    if not isinstance(field, str) or not field:
        raise ValueError("Expected numeric field name")
    if group_by is not None and (not isinstance(group_by, str) or not group_by):
        raise ValueError("Expected group field name")
    groups = {}
    for record in records:
        if not isinstance(record, dict) or field not in record:
            raise ValueError("Missing numeric field")
        if group_by is not None and group_by not in record:
            raise ValueError("Missing group field")
        key = record[group_by] if group_by else None
        if key is not None and not isinstance(key, str):
            raise ValueError("Group values must be strings or null")
        groups.setdefault(key, []).append(_number(record[field]))
    result = []
    # Bounds above ensure addition is exact; averages are rounded to 50 digits.
    with localcontext() as context:
        context.prec = 310
        for key, values in groups.items():
            total = sum(values, Decimal(0))
            with localcontext() as average_context:
                average_context.prec = 50
                average = total / len(values)
            result.append(
                {
                    "group": key,
                    "count": len(values),
                    "sum": str(total),
                    "average": str(average),
                    "min": str(min(values)),
                    "max": str(max(values)),
                }
            )
    return {"groups": result, "count": len(records)}
