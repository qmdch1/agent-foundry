from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .security import PolicyError, mask


class Database:
    def __init__(self, settings):
        self.pool = AsyncConnectionPool(
            settings.database_url.get_secret_value(),
            min_size=1,
            max_size=settings.db_pool_max,
            open=False,
            kwargs={"row_factory": dict_row},
            timeout=10,
        )

    async def open(self):
        await self.pool.open(wait=True)

    async def close(self):
        await self.pool.close()

    async def migrate(self):
        async with self.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('agent-schema', 0))")
            await conn.execute(Path(__file__).with_name("schema.sql").read_text())

    async def fetch(self, query, params=()):
        async with self.pool.connection() as conn:
            return await (await conn.execute(query, params)).fetchall()

    async def execute(self, query, params=()):
        async with self.pool.connection() as conn:
            return (await conn.execute(query, params)).rowcount

    @asynccontextmanager
    async def lock(self, name: str):
        # A session lock is kept across awaits; process death releases it automatically.
        async with self.pool.connection() as conn:
            await conn.set_autocommit(True)
            row = await (
                await conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired", (name,)
                )
            ).fetchone()
            if not row["acquired"]:
                await conn.set_autocommit(False)
                raise PolicyError("Another worker owns this deployment")
            try:
                yield
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (name,))
                await conn.set_autocommit(False)

    async def event(self, event_type, data, request_id=None):
        def sanitize(value):
            if isinstance(value, str):
                return mask(value)
            if isinstance(value, dict):
                return {k: sanitize(v) for k, v in value.items()}
            if isinstance(value, list):
                return [sanitize(v) for v in value]
            return value

        await self.execute(
            "INSERT INTO agent.events(id, request_id, event_type, data) VALUES (%s,%s,%s,%s)",
            (uuid4(), request_id, event_type, Jsonb(sanitize(data))),
        )
