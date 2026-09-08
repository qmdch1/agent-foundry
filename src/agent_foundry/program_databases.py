import base64
import hashlib
import hmac
import json
import secrets
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .security import PolicyError, decrypt_payload, encrypt_payload


def password_verifier(password):
    """Give PostgreSQL a SCRAM verifier, never a plaintext password in DDL/logs."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 4096)
    stored = hashlib.sha256(hmac.digest(salted, b"Client Key", "sha256")).digest()
    server = hmac.digest(salted, b"Server Key", "sha256")

    def encode(data):
        return base64.b64encode(data).decode()

    return f"SCRAM-SHA-256$4096:{encode(salt)}${encode(stored)}:{encode(server)}"


class ProgramDatabases:
    """One central database, one managed schema/least-privilege login per stateful tool."""

    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    async def database_name(self, conn):
        return (await (await conn.execute("SELECT current_database() AS name")).fetchone())["name"]

    async def tables(self, conn, manifest, schema):
        await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        await conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        types = {
            "text": "text",
            "integer": "integer",
            "bigint": "bigint",
            "numeric": "numeric",
            "boolean": "boolean",
            "jsonb": "jsonb",
            "timestamptz": "timestamp with time zone",
        }
        for table in manifest.tables:
            columns = sql.SQL(",").join(
                sql.SQL("{} {}").format(sql.Identifier(k), sql.SQL(v)) for k, v in table.columns.items()
            )
            await conn.execute(
                sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
                    sql.Identifier(schema), sql.Identifier(table.name), columns
                )
            )
            actual = await (
                await conn.execute(
                    "SELECT column_name,data_type FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                    (schema, table.name),
                )
            ).fetchall()
            known = {r["column_name"]: r["data_type"] for r in actual}
            for name, kind in table.columns.items():
                if name in known and known[name] != types[kind]:
                    raise PolicyError("Existing column type differs; destructive migration refused")
                if name not in known:
                    await conn.execute(
                        sql.SQL("ALTER TABLE {}.{} ADD COLUMN {} {}").format(
                            sql.Identifier(schema),
                            sql.Identifier(table.name),
                            sql.Identifier(name),
                            sql.SQL(kind),
                        )
                    )
            for index in table.indexes:
                name = "idx_" + hashlib.sha256((table.name + str(index)).encode()).hexdigest()[:24]
                await conn.execute(
                    sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
                        sql.Identifier(name),
                        sql.Identifier(schema),
                        sql.Identifier(table.name),
                        sql.SQL(",").join(map(sql.Identifier, index)),
                    )
                )

    async def role(self, conn, database, schema, role, password):
        marker = f"agent-foundry:{database}:{schema}"
        rows = await (
            await conn.execute(
                "SELECT oid,shobj_description(oid,'pg_authid') AS marker FROM pg_roles WHERE rolname=%s",
                (role,),
            )
        ).fetchall()
        if rows and rows[0]["marker"] != marker:
            raise PolicyError("Database role is not owned by this managed program binding")
        if (
            rows
            and await (
                await conn.execute("SELECT 1 FROM pg_auth_members WHERE member=%s", (rows[0]["oid"],))
            ).fetchone()
        ):
            raise PolicyError("Unexpected database role membership; refusing runtime access")
        operation = "ALTER" if rows else "CREATE"
        await conn.execute(
            sql.SQL(
                operation
                + " ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {} PASSWORD {}"
            ).format(
                sql.Identifier(role),
                sql.Literal(self.settings.tool_database_connection_limit),
                sql.Literal(password_verifier(password)),
            )
        )
        await conn.execute(
            sql.SQL("COMMENT ON ROLE {} IS {}").format(sql.Identifier(role), sql.Literal(marker))
        )
        # PUBLIC otherwise grants TEMP database access. This database is the dedicated Foundry database.
        await conn.execute(
            sql.SQL("REVOKE CREATE,TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        await conn.execute("REVOKE ALL ON SCHEMA agent,public FROM PUBLIC")
        await conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(role)
            )
        )
        await conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role))
        )
        await conn.execute(
            sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        for name, value in {
            "statement_timeout": self.settings.tool_database_statement_timeout_ms,
            "lock_timeout": self.settings.tool_database_lock_timeout_ms,
            "idle_in_transaction_session_timeout": self.settings.tool_database_statement_timeout_ms,
            "search_path": schema + ",pg_catalog",
        }.items():
            # search_path is a list, so pass its identifiers separately rather than one quoted string.
            setting = (
                sql.SQL("{},pg_catalog").format(sql.Identifier(schema))
                if name == "search_path"
                else sql.Literal(str(value))
            )
            await conn.execute(
                sql.SQL("ALTER ROLE {} IN DATABASE {} SET {} TO {}").format(
                    sql.Identifier(role), sql.Identifier(database), sql.Identifier(name), setting
                )
            )

    async def ensure(self, manifest, commit):
        if not manifest.requires_db:
            return None
        schema = "tool_" + manifest.program_id.hex
        checksum = hashlib.sha256(
            json.dumps([t.model_dump() for t in manifest.tables], sort_keys=True).encode()
        ).hexdigest()
        await self.db.event(
            "program_database_provision_started",
            {"program_id": str(manifest.program_id), "schema": schema, "git_commit": commit},
        )
        async with self.db.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (schema,))
            database = await self.database_name(conn)
            role = "afr_" + hashlib.sha256((database + ":" + schema).encode()).hexdigest()[:32]
            rows = await (
                await conn.execute(
                    "SELECT * FROM agent.program_databases WHERE program_id=%s FOR UPDATE",
                    (manifest.program_id,),
                )
            ).fetchall()
            if len(rows) > 1:
                raise PolicyError("Duplicate program database binding")
            password = (
                decrypt_payload(
                    rows[0]["encrypted_credentials"], self.settings.job_encryption_key.get_secret_value()
                )["password"]
                if rows
                else secrets.token_urlsafe(32)
            )
            if rows and (rows[0]["schema_name"] != schema or rows[0]["role_name"] != role):
                raise PolicyError("Stored central database binding differs from this database")
            await self.tables(conn, manifest, schema)
            await self.role(conn, database, schema, role, password)
            if rows:
                await conn.execute(
                    "UPDATE agent.program_databases SET status='READY',updated_at=now() WHERE program_id=%s",
                    (manifest.program_id,),
                )
            else:
                encrypted = encrypt_payload(
                    {"password": password}, self.settings.job_encryption_key.get_secret_value()
                )
                await conn.execute(
                    """INSERT INTO agent.program_databases(program_id,schema_name,role_name,secret_name,encrypted_credentials)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (manifest.program_id, schema, role, "central-db:" + str(manifest.program_id), encrypted),
                )
            await conn.execute(
                """INSERT INTO agent.tool_migrations(program_id,checksum,git_commit)
                SELECT %s,%s,%s WHERE NOT EXISTS (SELECT 1 FROM agent.tool_migrations WHERE program_id=%s AND checksum=%s)""",
                (manifest.program_id, checksum, commit, manifest.program_id, checksum),
            )
        await self.check(manifest.program_id)
        await self.db.event(
            "program_database_ready",
            {
                "program_id": str(manifest.program_id),
                "schema": schema,
                "role": role,
                "checksum": checksum,
                "git_commit": commit,
            },
        )
        return await self.public(manifest.program_id)

    async def public(self, program_id):
        rows = await self.db.fetch(
            "SELECT connection_name,schema_name,role_name,secret_name,status,created_at,updated_at FROM agent.program_databases WHERE program_id=%s",
            (program_id,),
        )
        return rows[0] if len(rows) == 1 else None

    def environment(self, database, schema, role, password, *, inside_container=True):
        base = conninfo_to_dict(self.settings.database_url.get_secret_value())
        connection = {
            "dbname": database,
            "user": role,
            "password": password,
            "connect_timeout": 5,
            "host": self.settings.tool_database_host if inside_container else base.get("host", "localhost"),
            "port": self.settings.tool_database_port if inside_container else base.get("port", 5432),
            "sslmode": base.get("sslmode", "prefer"),
            "options": "-c search_path=" + schema + ",pg_catalog",
        }
        return {"FOUNDRY_TOOL_DATABASE_URL": make_conninfo(**connection), "FOUNDRY_TOOL_SCHEMA": schema}

    async def runtime(self, program_id, *, inside_container=True):
        rows = await self.db.fetch(
            "SELECT *,current_database() AS database_name FROM agent.program_databases WHERE program_id=%s AND status='READY'",
            (program_id,),
        )
        if len(rows) != 1:
            raise PolicyError("Program database is not provisioned")
        row = rows[0]
        if row["connection_name"] != "central" or row["schema_name"] != "tool_" + UUID(str(program_id)).hex:
            raise PolicyError("Invalid central program schema")
        password = decrypt_payload(
            row["encrypted_credentials"], self.settings.job_encryption_key.get_secret_value()
        )["password"]
        return self.environment(
            row["database_name"],
            row["schema_name"],
            row["role_name"],
            password,
            inside_container=inside_container,
        )

    async def check(self, program_id):
        env = await self.runtime(program_id, inside_container=False)
        async with await AsyncConnection.connect(env["FOUNDRY_TOOL_DATABASE_URL"]) as conn:
            row = await (await conn.execute("SELECT current_schema()")).fetchone()
            if row[0] != env["FOUNDRY_TOOL_SCHEMA"]:
                raise PolicyError("Program database search path differs from its binding")

    @asynccontextmanager
    async def test_scope(self, manifest):
        scope = uuid4()
        schema, role, password = "test_tool_" + scope.hex, "aft_" + scope.hex, secrets.token_urlsafe(32)
        async with self.db.pool.connection() as conn:
            database = await self.database_name(conn)
            await self.tables(conn, manifest, schema)
            await self.role(conn, database, schema, role, password)
            await conn.execute(
                "INSERT INTO agent.database_test_scopes(id,schema_name,role_name,expires_at) VALUES (%s,%s,%s,now()+interval '2 hours')",
                (scope, schema, role),
            )
        await self.db.event(
            "database_test_scope_created", {"scope_id": str(scope), "program_id": str(manifest.program_id)}
        )
        try:
            yield self.environment(database, schema, role, password)
        finally:
            await self.remove_test_scope(scope, schema, role)

    async def remove_test_scope(self, scope, schema, role):
        scope = UUID(str(scope))
        if schema != "test_tool_" + scope.hex or role != "aft_" + scope.hex:
            raise PolicyError("Only an exact platform-created test scope may be removed")
        async with self.db.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT id FROM agent.database_test_scopes WHERE id=%s AND schema_name=%s AND role_name=%s FOR UPDATE",
                    (scope, schema, role),
                )
            ).fetchall()
            if len(rows) != 1:
                return
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename=%s AND pid<>pg_backend_pid()",
                (role,),
            )
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            database = await self.database_name(conn)
            await conn.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
            await conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
            await conn.execute("DELETE FROM agent.database_test_scopes WHERE id=%s", (scope,))
        await self.db.event("database_test_scope_removed", {"scope_id": str(scope)})

    async def reap_tests(self):
        for row in await self.db.fetch("SELECT * FROM agent.database_test_scopes WHERE expires_at<now()"):
            await self.remove_test_scope(row["id"], row["schema_name"], row["role_name"])
