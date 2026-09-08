from psycopg.types.json import Jsonb

from .models import Manifest
from .security import PolicyError


class Registry:
    def __init__(self, db):
        self.db = db

    async def get(self, program_id, active=True):
        rows = await self.db.fetch("SELECT * FROM agent.programs WHERE id=%s", (program_id,))
        if len(rows) != 1 or active and rows[0]["status"] != "ACTIVE":
            raise PolicyError("Program is not uniquely registered and active")
        return rows[0]

    async def register(self, manifest: Manifest, *, status, repository="", path="", commit="", evidence=None):
        if status == "ACTIVE" and manifest.runtime == "python":
            import re

            if not re.fullmatch(r"[0-9a-f]{40}", commit) or not evidence or not evidence.get("passed"):
                raise PolicyError("Active Python programs require a pinned commit and passing evidence")
        m = manifest.model_dump(mode="json")
        search_text = " ".join(
            [manifest.name, manifest.description, *manifest.tags, *(e.prompt for e in manifest.examples)]
        ).lower()
        values = (
            manifest.program_id,
            manifest.name,
            manifest.description,
            manifest.version,
            "tool",
            manifest.runtime,
            manifest.execution_type,
            manifest.endpoint,
            manifest.method,
            manifest.entrypoint,
            repository,
            path,
            commit,
            Jsonb(manifest.input_schema),
            Jsonb(manifest.output_schema),
            Jsonb(m["selection_rules"]),
            manifest.tags,
            Jsonb(m["examples"]),
            status,
            Jsonb(m),
            search_text,
            search_text,
        )
        async with self.db.pool.connection() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(manifest.program_id),)
            )
            if status == "ACTIVE" and manifest.requires_db:
                bindings = await (
                    await conn.execute(
                        "SELECT schema_name FROM agent.program_databases WHERE program_id=%s AND status='READY'",
                        (manifest.program_id,),
                    )
                ).fetchall()
                if len(bindings) != 1 or bindings[0]["schema_name"] != "tool_" + manifest.program_id.hex:
                    raise PolicyError("Active database tools require a ready central schema binding")
            old = await (
                await conn.execute(
                    "SELECT * FROM agent.programs WHERE id=%s FOR UPDATE", (manifest.program_id,)
                )
            ).fetchall()
            if len(old) > 1:
                raise PolicyError("Duplicate registry identity")
            # Keep the row identity stable so concurrent executions cannot lose counter updates.
            if old:
                await conn.execute(
                    """UPDATE agent.programs SET name=%s,description=%s,version=%s,
                    program_type=%s,runtime=%s,execution_type=%s,endpoint=%s,method=%s,entrypoint=%s,
                    repository=%s,repository_path=%s,git_commit=%s,input_schema=%s,output_schema=%s,
                    selection_rules=%s,tags=%s,examples=%s,status=%s,manifest=%s,search_text=%s,
                    search_vector=to_tsvector('simple',%s),updated_at=now() WHERE id=%s""",
                    (*values[1:], manifest.program_id),
                )
            else:
                await conn.execute(
                    """INSERT INTO agent.programs
                    (id,name,description,version,program_type,runtime,execution_type,endpoint,method,entrypoint,
                     repository,repository_path,git_commit,input_schema,output_schema,selection_rules,tags,
                     examples,status,manifest,search_text,search_vector)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            to_tsvector('simple',%s))""",
                    values,
                )
            if status == "ACTIVE":
                await conn.execute(
                    "UPDATE agent.programs SET installed_at=COALESCE(installed_at,now()),last_deployed_at=now() WHERE id=%s",
                    (manifest.program_id,),
                )
            if status == "ACTIVE" and commit:
                await conn.execute(
                    """INSERT INTO agent.releases
                    (program_id,version,git_commit,manifest,repository,repository_path,evidence)
                    SELECT %s,%s,%s,%s,%s,%s,%s WHERE NOT EXISTS
                    (SELECT 1 FROM agent.releases WHERE program_id=%s AND git_commit=%s)""",
                    (
                        manifest.program_id,
                        manifest.version,
                        commit,
                        Jsonb(m),
                        repository,
                        path,
                        Jsonb(evidence or {}),
                        manifest.program_id,
                        commit,
                    ),
                )

    async def record_execution(self, program_id, success, duration):
        await self.db.execute(
            """UPDATE agent.programs SET
            avg_latency_ms=(avg_latency_ms*usage_count+%s)/(usage_count+1), usage_count=usage_count+1,
            success_count=success_count+%s, failure_count=failure_count+%s,last_used_at=now()
            WHERE id=%s""",
            (duration, int(success), int(not success), program_id),
        )

    async def active(self):
        return await self.db.fetch("SELECT * FROM agent.programs WHERE status='ACTIVE' ORDER BY name")

    async def health_report(self):
        return await self.db.fetch("""SELECT id,name,version,git_commit,status,usage_count,success_count,
            failure_count,avg_latency_ms,last_used_at,description,installed_at,last_deployed_at,
            estimated_tokens_saved,attributed_llm_tokens,savings_sample_count,
            CASE WHEN usage_count=0 THEN 'unused'
                 WHEN failure_count::float/usage_count > 0.2 THEN 'high_failure_rate'
                 ELSE 'observing' END AS observation
            FROM agent.programs ORDER BY failure_count DESC, usage_count ASC LIMIT 200""")
