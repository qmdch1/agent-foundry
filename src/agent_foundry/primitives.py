import ast
import json
import operator
import os
from decimal import Decimal, localcontext

import psycopg
from psycopg.rows import dict_row

from .security import PolicyError, safe_path


def calculate(expression: str) -> dict:
    if len(expression) > 300:
        raise PolicyError("Expression is too long")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 100:
        raise PolicyError("Expression is too complex")
    operations = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
    }

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Decimal(ast.get_source_segment(expression, node))
        if isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub):
            return visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        if isinstance(node, ast.BinOp) and type(node.op) in operations:
            return operations[type(node.op)](visit(node.left), visit(node.right))
        raise PolicyError("Only bounded numeric arithmetic is supported")

    with localcontext() as context:
        context.prec = 50
        result = visit(tree.body)
        if not result.is_finite() or result.adjusted() > 500:
            raise PolicyError("Numeric result is outside limits")
        return {"result": format(result.normalize(), "f")}


async def builtin(name, data, settings):
    if name == "calculator":
        return calculate(data["expression"])
    if name == "file-reader":
        path = safe_path(settings.file_root, data["path"])
        with open(path, "rb") as stream:
            value = stream.read(settings.max_output_bytes + 1)
        if len(value) > settings.max_output_bytes:
            raise PolicyError("File is too large")
        return {"content": value.decode("utf-8")}
    if name == "file-writer":
        path = safe_path(settings.file_root, data["path"])
        value = data["content"].encode()
        if len(value) > settings.max_output_bytes:
            raise PolicyError("File is too large")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation protects existing files; replacements require a separate policy.
        with open(path, "xb") as stream:
            stream.write(value)
        return {"path": data["path"], "bytes": len(value)}
    if name == "database-query":
        query = settings.database_queries.get(data["query_id"])
        if not query or not query.lstrip().upper().startswith("SELECT ") or ";" in query:
            raise PolicyError("Only administrator-approved single SELECT templates may run")
        dsn = os.environ.get(settings.query_database_secret)
        if not dsn:
            raise PolicyError("Read-only query connection is not configured")
        async with await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row) as conn:
            await conn.execute("SET TRANSACTION READ ONLY")
            await conn.execute(
                "SELECT set_config('statement_timeout',%s,true)",
                (str(int(settings.execution_timeout * 1000)),),
            )
            async with conn.cursor(name="bounded_query") as cursor:
                await cursor.execute(query, data.get("parameters", {}))
                rows = await cursor.fetchmany(1001)
                if len(rows) > 1000:
                    raise PolicyError("Query exceeds the 1000-row response limit")
                return {"rows": json.loads(json.dumps(rows, default=str))}
    raise PolicyError("Internal primitive is not executable through the user API")
