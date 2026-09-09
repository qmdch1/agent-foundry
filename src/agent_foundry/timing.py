"""Local, structured stage timing; callers supply identifiers, never request contents."""

from contextlib import asynccontextmanager
from time import perf_counter


@asynccontextmanager
async def stage(db, name, request_id=None, **metadata):
    details = dict(metadata)
    started = perf_counter()
    success = False
    try:
        yield details
        success = True
    except BaseException as exc:
        details["error_type"] = type(exc).__name__
        raise
    finally:
        if db is not None:
            await db.event(
                "pipeline_stage",
                {
                    **details,
                    "stage": name,
                    "success": success,
                    "duration_ms": round((perf_counter() - started) * 1000, 3),
                },
                request_id,
            )
