import re
from pathlib import Path

from psycopg.types.json import Jsonb

from .models import Manifest
from .search import ProgramSearch
from .security import PolicyError


class Catalog:
    """Published Git metadata, separate from programs installed on this platform."""

    def __init__(self, db, commands, deployment, router, settings):
        self.db, self.commands, self.deployment = db, commands, deployment
        self.router, self.settings = router, settings
        self.search = ProgramSearch(db, settings, catalog=True)

    async def sync(self):
        if not self.settings.catalog_enabled:
            return {"disabled": True}
        async with self.db.lock("catalog-sync"):
            cache = self.settings.state_root.resolve() / "catalog-git"
            cache.parent.mkdir(parents=True, exist_ok=True)
            repository = self.settings.tool_repository
            if not cache.exists():
                await self.commands.run(
                    [
                        "git",
                        "clone",
                        "--bare",
                        "--single-branch",
                        "--branch",
                        self.settings.git_branch,
                        repository,
                        str(cache),
                    ],
                    timeout=self.settings.build_timeout,
                )
            remote = await self.commands.run(["git", "--git-dir", str(cache), "remote", "get-url", "origin"])
            if remote.decode().strip() != repository:
                raise PolicyError("Catalog remote differs from the approved repository")
            # Only fetched remote commits are indexed; a local unpushed working tree is never published.
            await self.commands.run(
                [
                    "git",
                    "--git-dir",
                    str(cache),
                    "fetch",
                    "origin",
                    f"+refs/heads/{self.settings.git_branch}:refs/heads/catalog-published",
                ],
                timeout=self.settings.build_timeout,
            )

            async def git(*args, limit=1_000_000):
                return await self.commands.run(["git", "--git-dir", str(cache), *args], limit=limit)

            head = (await git("rev-parse", "refs/heads/catalog-published")).decode().strip()
            previous = await self.db.fetch(
                "SELECT git_commit FROM agent.catalog_sync WHERE repository=%s", (repository,)
            )
            if previous and previous[0]["git_commit"] == head:
                await self.db.execute(
                    "UPDATE agent.catalog_sync SET synced_at=now() WHERE repository=%s", (repository,)
                )
                return {"git_commit": head, "changed": False}
            old = {
                r["repository_path"]: r
                for r in await self.db.fetch("SELECT * FROM agent.catalog WHERE repository=%s", (repository,))
            }
            trees = []
            if (await git("ls-tree", "-d", head, "--", "tools")).strip():
                trees = (await git("ls-tree", "-d", "-z", head + ":tools", limit=8_000_000)).split(b"\0")
            rows = []
            for entry in filter(None, trees):
                metadata, name = entry.decode().split("\t", 1)
                mode, kind, tree = metadata.split()
                if not re.fullmatch(r"[a-z][a-z0-9-]{2,47}", name):
                    continue
                if kind != "tree" or mode != "040000":
                    raise PolicyError("Catalog tools must be ordinary source directories")
                path = "tools/" + name
                if path in old and old[path]["source_tree"] == tree:
                    row = old[path]
                    m = Manifest.model_validate(row["manifest"])
                    commit, published = row["git_commit"], row["published_at"]
                else:
                    raw = await git("show", f"{head}:{path}/manifest.json", limit=128_000)
                    m = Manifest.model_validate_json(raw)
                    if m.name != name:
                        raise PolicyError("Catalog folder and manifest name differ")
                    commit, published = (
                        (await git("log", "-1", "--format=%H%n%cI", head, "--", path))
                        .decode()
                        .strip()
                        .splitlines()
                    )
                # Automatic imports use the same offline, public, non-destructive policy as generated tools.
                if m.runtime != "python" or m.visibility != "public" or m.side_effects or m.requires_db:
                    continue
                text = " ".join([m.name, m.description, *m.tags, *(e.prompt for e in m.examples)]).lower()
                rows.append(
                    (
                        m.program_id,
                        m.name,
                        m.description,
                        m.version,
                        m.runtime,
                        m.execution_type,
                        Jsonb(m.model_dump(mode="json")),
                        Jsonb(m.input_schema),
                        Jsonb(m.output_schema),
                        m.tags,
                        Jsonb([e.model_dump() for e in m.examples]),
                        text,
                        text,
                        repository,
                        path,
                        commit,
                        tree,
                        published,
                    )
                )
                if len(rows) > self.settings.catalog_max_programs:
                    raise PolicyError("Catalog exceeds configured program limit")
            # Publish the complete validated metadata snapshot atomically, retaining the previous one on failure.
            async with self.db.pool.connection() as conn:
                await conn.execute("DELETE FROM agent.catalog")
                async with conn.cursor() as cursor:
                    await cursor.executemany(
                        """INSERT INTO agent.catalog
                        (id,name,description,version,runtime,execution_type,manifest,input_schema,output_schema,
                         tags,examples,search_text,search_vector,repository,repository_path,git_commit,source_tree,published_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s,%s,%s,%s,%s)""",
                        rows,
                    )
                await conn.execute("DELETE FROM agent.catalog_sync")
                await conn.execute(
                    "INSERT INTO agent.catalog_sync(repository,git_commit) VALUES (%s,%s)", (repository, head)
                )
            await self.db.event("catalog_synced", {"git_commit": head, "programs": len(rows)})
            return {"git_commit": head, "programs": len(rows), "changed": True}

    async def references(self, plan):
        refs = []
        for step in plan.programs:
            rows = await self.db.fetch(
                "SELECT id,repository,repository_path,git_commit FROM agent.catalog WHERE id=%s",
                (step.program_id,),
            )
            if len(rows) != 1:
                raise PolicyError("Published program is unavailable")
            refs.append({**rows[0], "id": str(rows[0]["id"])})
        return refs

    async def install(self, references):
        results = []
        async with self.db.lock("tool-repository-deployment"):
            for ref in references:
                existing = await self.db.fetch("SELECT * FROM agent.programs WHERE id=%s", (ref["id"],))
                if existing:
                    if existing[0]["status"] != "ACTIVE":
                        raise PolicyError(
                            "An administrator disabled this program; automatic activation refused"
                        )
                    receipt = (
                        self.settings.state_root
                        / "deployments"
                        / ref["id"]
                        / f"{existing[0]['git_commit']}.json"
                    )
                    if receipt.is_file():
                        results.append({"program_id": ref["id"], "status": "ALREADY_INSTALLED"})
                        continue
                    ref = {
                        **ref,
                        "repository": existing[0]["repository"],
                        "git_commit": existing[0]["git_commit"],
                        "repository_path": existing[0]["repository_path"],
                    }
                directory = await self.deployment.checkout(
                    ref["repository"], ref["git_commit"], ref["repository_path"]
                )
                manifest = Manifest.model_validate_json((Path(directory) / "manifest.json").read_text())
                if (
                    str(manifest.program_id) != ref["id"]
                    or manifest.runtime != "python"
                    or manifest.visibility != "public"
                    or manifest.side_effects
                    or manifest.requires_db
                ):
                    raise PolicyError("Published program is outside automatic installation policy")
                await self.deployment.deploy(manifest, directory, ref["git_commit"])
                results.append({"program_id": ref["id"], "git_commit": ref["git_commit"], "status": "ACTIVE"})
        return "SUCCEEDED", {"programs": results}

    async def reuse(self, prompt, request_id=None):
        """Worker-only fresh Git check before evaluating or generating anything."""
        await self.sync()
        candidates = await self.search.search(prompt)
        plan, _ = await self.router.route(prompt, candidates, request_id)
        if plan.action == "execute":
            return await self.install(await self.references(plan))
        if candidates:
            return "SKIPPED", {
                "reason": "published_extension_review",
                "candidates": [str(c.program_id) for c in candidates],
            }
        return None
