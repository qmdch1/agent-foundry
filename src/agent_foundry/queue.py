from uuid import uuid4

from psycopg.types.json import Jsonb

from .security import PolicyError, decrypt_payload, encrypt_payload, mask


class JobQueue:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    async def enqueue(self, kind, fingerprint, payload, delay=0, *, dedup_seconds=86400):
        encrypted = encrypt_payload(payload, self.settings.job_encryption_key.get_secret_value())
        async with self.db.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (kind + fingerprint,))
            existing = await (
                await conn.execute(
                    """SELECT id FROM agent.jobs WHERE kind=%s AND fingerprint=%s
                AND (status IN ('PENDING','RUNNING') OR
                     (status IN ('SUCCEEDED','SKIPPED') AND updated_at>now()-%s*interval '1 second')) LIMIT 1""",
                    (kind, fingerprint, dedup_seconds),
                )
            ).fetchone()
            if existing:
                return existing["id"]
            job_id = uuid4()
            await conn.execute(
                """INSERT INTO agent.jobs(id,kind,fingerprint,payload,available_at)
                VALUES (%s,%s,%s,%s,now()+%s*interval '1 second')""",
                (job_id, kind, fingerprint, encrypted, delay),
            )
            return job_id

    async def claim(self):
        owner = uuid4()
        async with self.db.pool.connection() as conn:
            await conn.execute(
                """UPDATE agent.jobs SET status='FAILED',payload=NULL,error='Worker retry limit',
                updated_at=now() WHERE status='RUNNING' AND lease_until<now() AND attempts>=%s""",
                (self.settings.job_max_attempts,),
            )
            row = await (
                await conn.execute(
                    """WITH selected AS (
                SELECT id FROM agent.jobs WHERE
                  (status='PENDING' AND available_at<=now()) OR
                  (status='RUNNING' AND lease_until<now() AND attempts<%s)
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE agent.jobs j SET status='RUNNING',owner=%s,attempts=attempts+1,
                  lease_until=now()+%s*interval '1 second',updated_at=now()
                FROM selected WHERE j.id=selected.id RETURNING j.*""",
                    (self.settings.job_max_attempts, owner, self.settings.job_lease_seconds),
                )
            ).fetchone()
            return row

    def payload(self, job):
        return decrypt_payload(job["payload"], self.settings.job_encryption_key.get_secret_value())

    async def heartbeat(self, job):
        count = await self.db.execute(
            """UPDATE agent.jobs SET lease_until=now()+%s*interval '1 second',
            updated_at=now() WHERE id=%s AND owner=%s AND status='RUNNING' AND lease_until>now()""",
            (self.settings.job_lease_seconds, job["id"], job["owner"]),
        )
        if count != 1:
            raise PolicyError("Worker lease lost")

    async def finish(self, job, status, result=None, error=None):
        count = await self.db.execute(
            """UPDATE agent.jobs SET status=%s,result=%s,error=%s,payload=NULL,
            lease_until=NULL,updated_at=now() WHERE id=%s AND owner=%s AND status='RUNNING'
            AND lease_until>now()""",
            (status, Jsonb(result or {}), mask(error or ""), job["id"], job["owner"]),
        )
        if count != 1:
            raise PolicyError("Worker lease lost before completion")

    async def get(self, job_id):
        rows = await self.db.fetch(
            """SELECT id,kind,status,attempts,result,error,created_at,updated_at
            FROM agent.jobs WHERE id=%s""",
            (job_id,),
        )
        return rows[0] if rows else None
