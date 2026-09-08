import hashlib
import json
import re
import tarfile
import tempfile
from pathlib import Path

from psycopg import sql

from .models import Manifest
from .security import PolicyError, safe_path


class Deployment:
    def __init__(self, registry, sandbox, commands, settings):
        self.registry, self.sandbox, self.commands, self.settings = registry, sandbox, commands, settings

    async def checkout(self, repository, commit, repository_path):
        if repository != self.settings.tool_repository or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise PolicyError("Only the approved repository and full immutable commits are accepted")
        if not re.fullmatch(r"tools/[a-z][a-z0-9-]{2,47}", repository_path):
            raise PolicyError("Repository path must identify one tool directory")
        cache = self.settings.state_root.resolve() / "git-cache"
        cache.parent.mkdir(parents=True, exist_ok=True)
        if not cache.exists():
            await self.commands.run(
                ["git", "clone", "--mirror", repository, str(cache)], timeout=self.settings.build_timeout
            )
        else:
            remote = await self.commands.run(["git", "--git-dir", str(cache), "remote", "get-url", "origin"])
            if remote.decode().strip() != repository:
                raise PolicyError("Git cache remote differs from approved repository")
            await self.commands.run(
                ["git", "--git-dir", str(cache), "fetch", "origin"], timeout=self.settings.build_timeout
            )
        raw = await self.commands.run(
            ["git", "--git-dir", str(cache), "archive", commit, "--", repository_path], limit=3_000_000
        )
        root = self.settings.state_root.resolve() / "releases" / commit
        root.mkdir(parents=True, exist_ok=True)
        import io

        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for member in archive.getmembers():
                if not member.isfile() and not member.isdir():
                    raise PolicyError("Git archive contains a link or special file")
                safe_path(root, member.name.rstrip("/"))
            archive.extractall(root, filter="data")
        return safe_path(root, repository_path)

    async def apply_tables(self, manifest, commit):
        if not manifest.tables:
            return
        # Additive, platform-generated SQL only. No model-authored SQL is executed.
        checksum = hashlib.sha256(
            json.dumps([t.model_dump() for t in manifest.tables], sort_keys=True).encode()
        ).hexdigest()
        schema = "tool_" + manifest.program_id.hex
        async with self.registry.db.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (schema,))
            if await (
                await conn.execute(
                    "SELECT 1 FROM agent.tool_migrations WHERE program_id=%s AND checksum=%s",
                    (manifest.program_id, checksum),
                )
            ).fetchone():
                return
            await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            for table in manifest.tables:
                columns = sql.SQL(",").join(
                    sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL(kind))
                    for name, kind in table.columns.items()
                )
                await conn.execute(
                    sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
                        sql.Identifier(schema), sql.Identifier(table.name), columns
                    )
                )
                actual = await (
                    await conn.execute(
                        """SELECT column_name,data_type FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=%s""",
                        (schema, table.name),
                    )
                ).fetchall()
                types = {
                    "text": "text",
                    "integer": "integer",
                    "bigint": "bigint",
                    "numeric": "numeric",
                    "boolean": "boolean",
                    "jsonb": "jsonb",
                    "timestamptz": "timestamp with time zone",
                }
                known = {c["column_name"]: c["data_type"] for c in actual}
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
                    index_name = "idx_" + hashlib.sha256((table.name + str(index)).encode()).hexdigest()[:24]
                    await conn.execute(
                        sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} ({})").format(
                            sql.Identifier(index_name),
                            sql.Identifier(schema),
                            sql.Identifier(table.name),
                            sql.SQL(",").join(map(sql.Identifier, index)),
                        )
                    )
            await conn.execute(
                "INSERT INTO agent.tool_migrations(program_id,checksum,git_commit) VALUES (%s,%s,%s)",
                (manifest.program_id, checksum, commit),
            )
        await self.registry.db.event(
            "additive_migration",
            {"program_id": str(manifest.program_id), "git_commit": commit, "checksum": checksum},
        )

    async def deploy(self, manifest, directory, commit, *, activate=True):
        image = await self.sandbox.build(directory, manifest)
        evidence = await self.sandbox.validate(image, manifest)
        await self.apply_tables(manifest, commit)
        receipt = {
            "image": image,
            "git_commit": commit,
            "manifest": manifest.model_dump(mode="json"),
            "evidence": evidence,
        }
        parent = self.settings.state_root / "deployments" / str(manifest.program_id)
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=parent, delete=False, suffix=".tmp") as stream:
            json.dump(receipt, stream)
            temporary = Path(stream.name)
        temporary.replace(parent / f"{commit}.json")
        if activate:
            await self.registry.register(
                manifest,
                status="ACTIVE",
                repository=self.settings.tool_repository,
                path=f"tools/{manifest.name}",
                commit=commit,
                evidence=evidence,
            )
        await self.registry.db.event(
            "deployment",
            {
                "program_id": str(manifest.program_id),
                "git_commit": commit,
                "passed": True,
                "activated": activate,
            },
        )
        return receipt

    async def reconcile(self):
        results = []
        async with self.registry.db.lock("tool-repository-deployment"):
            for row in await self.registry.active():
                if row["runtime"] != "python":
                    continue
                try:
                    directory = await self.checkout(
                        row["repository"], row["git_commit"], row["repository_path"]
                    )
                    manifest = Manifest.model_validate_json((directory / "manifest.json").read_text())
                    if manifest.model_dump(mode="json") != row["manifest"]:
                        raise PolicyError("Git manifest and Registry differ")
                    await self.deploy(manifest, directory, row["git_commit"], activate=False)
                    results.append({"program_id": str(row["id"]), "success": True})
                except Exception as exc:
                    results.append(
                        {"program_id": str(row["id"]), "success": False, "error": type(exc).__name__}
                    )
        return results

    async def rollback(self, program_id, commit):
        async with self.registry.db.lock("tool-repository-deployment"):
            rows = await self.registry.db.fetch(
                "SELECT * FROM agent.releases WHERE program_id=%s AND git_commit=%s", (program_id, commit)
            )
            if len(rows) != 1 or not rows[0]["evidence"].get("passed"):
                raise PolicyError("Rollback requires a previously tested stable release")
            row = rows[0]
            directory = await self.checkout(row["repository"], commit, row["repository_path"])
            manifest = Manifest.model_validate_json((directory / "manifest.json").read_text())
            if manifest.model_dump(mode="json") != row["manifest"]:
                raise PolicyError("Rollback manifest differs from stable release")
            receipt = await self.deploy(manifest, directory, commit)
            await self.registry.db.event("rollback", {"program_id": str(program_id), "git_commit": commit})
            return receipt
