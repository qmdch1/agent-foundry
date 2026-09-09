"""Foundry storage template v1.0.0; the Worker provisions the declared table."""

import json
import os
import re

import psycopg
from psycopg.types.json import Jsonb

TEMPLATE_VERSION = "1.0.0"


def store(input_data):
    """put/get/list bounded JSON records in the program's central DB schema."""
    if not isinstance(input_data, dict):
        raise ValueError("Expected object")
    operation = input_data.get("operation")
    allowed = {
        "put": {"operation", "key", "value"},
        "get": {"operation", "key"},
        "list": {"operation", "limit", "after"},
    }
    if operation not in allowed or set(input_data) - allowed[operation]:
        raise ValueError("Invalid storage operation or fields")
    key = input_data.get("key")
    if operation != "list" and (not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", key)):
        raise ValueError("Key must be 1..100 safe characters")
    if operation == "put":
        value = input_data.get("value")
        if not isinstance(value, dict) or len(json.dumps(value, allow_nan=False).encode()) > 100000:
            raise ValueError("Value must be a JSON object of at most 100000 bytes")
    limit, after = input_data.get("limit", 50), input_data.get("after", "")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Limit must be 1..100")
    if not isinstance(after, str) or len(after) > 100:
        raise ValueError("Invalid pagination cursor")
    action = "read"
    with psycopg.connect(os.environ["FOUNDRY_TOOL_DATABASE_URL"]) as connection:
        if operation == "list":
            rows = connection.execute(
                "SELECT record_key, payload FROM template_records WHERE record_key > %s "
                "ORDER BY record_key LIMIT %s",
                (after, limit),
            ).fetchall()
        else:
            if operation == "put":
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(current_schema() || ':' || %s, 0))",
                    (key,),
                )
            rows = connection.execute(
                "SELECT record_key, payload FROM template_records WHERE record_key=%s LIMIT 2",
                (key,),
            ).fetchall()
            if len(rows) > 1:
                raise ValueError("Duplicate record requires reconciliation")
            if operation == "put":
                action = "created" if not rows else "reused" if rows[0][1] == value else "updated"
                if action != "reused":
                    connection.execute("DELETE FROM template_records WHERE record_key=%s", (key,))
                    connection.execute(
                        "INSERT INTO template_records(record_key,payload) VALUES (%s,%s)",
                        (key, Jsonb(value)),
                    )
                rows = [(key, value)]
    # Context manager committed successfully before reporting the write outcome.
    return {
        "items": [{"key": row[0], "value": row[1]} for row in rows],
        "storage": {"action": action, "record_type": "프로그램 기록"},
    }
