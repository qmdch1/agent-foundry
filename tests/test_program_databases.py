import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from agent_foundry.api import create_app
from agent_foundry.models import Bundle, Manifest
from agent_foundry.security import PolicyError

pytestmark = pytest.mark.integration


def database_manifest():
    return Manifest(
        name="database-test-" + uuid4().hex[:12],
        version="1.0.0",
        description="Private persistent counter",
        requires_db=True,
        network="database",
        tables=[{"name": "records", "columns": {"value": "integer"}, "indexes": [["value"]]}],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        examples=[{"prompt": "counter sample", "input": {}, "output": {"count": 1}}],
    )


async def test_central_schema_login_isolation_and_preserved_upgrade(container):
    manager = container.deployment.databases
    one, two = database_manifest(), database_manifest()
    with pytest.raises(PolicyError, match="ready central schema"):
        await container.registry.register(one, status="ACTIVE", commit="a" * 40, evidence={"passed": True})
    first = await manager.ensure(one, "a" * 40)
    second = await manager.ensure(two, "b" * 40)
    env = await manager.runtime(one.program_id, inside_container=False)
    credentials = conninfo_to_dict(env["FOUNDRY_TOOL_DATABASE_URL"])
    assert credentials["user"] != conninfo_to_dict(container.settings.database_url.get_secret_value())["user"]
    assert first["schema_name"] != second["schema_name"]
    async with await psycopg.AsyncConnection.connect(
        env["FOUNDRY_TOOL_DATABASE_URL"], autocommit=True
    ) as conn:
        await conn.execute("INSERT INTO records(value) VALUES (42)")
        for statement in [
            "SELECT * FROM agent.programs",
            f'SELECT * FROM "{second["schema_name"]}".records',
            "CREATE TABLE forbidden(value integer)",
            "CREATE TEMP TABLE forbidden(value integer)",
            "TRUNCATE records",
            "DROP TABLE records",
        ]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute(statement)
        assert (await (await conn.execute("SELECT value FROM records")).fetchone())[0] == 42
    one.tables[0].columns["label"] = "text"
    updated = await manager.ensure(one, "c" * 40)
    assert updated["created_at"] == first["created_at"]
    assert await manager.runtime(one.program_id, inside_container=False) == env
    async with await psycopg.AsyncConnection.connect(env["FOUNDRY_TOOL_DATABASE_URL"]) as conn:
        assert await (await conn.execute("SELECT value,label FROM records")).fetchone() == (42, None)
    one.tables[0].columns["value"] = "text"
    with pytest.raises(PolicyError, match="destructive migration"):
        await manager.ensure(one, "d" * 40)
    stored = await container.db.fetch("SELECT encrypted_credentials FROM agent.program_databases")
    assert all(credentials["password"] not in r["encrypted_credentials"] for r in stored)
    events = await container.db.fetch("SELECT data FROM agent.events")
    assert credentials["password"] not in str(events)
    assert "encrypted_credentials" not in first
    await container.registry.register(one, status="DISABLED")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container.settings, container)), base_url="http://test"
    ) as client:
        await client.post("/ui/login", headers={"Origin": "http://test"}, json={"access_key": "test-admin"})
        response = await client.get("/ui/programs", params={"q": one.name})
        item = response.json()["items"][0]
        assert item["requires_db"] and item["database"]["schema_name"] == first["schema_name"]
        assert "encrypted_credentials" not in response.text and credentials["password"] not in response.text


async def test_disposable_test_schema_never_uses_production_data(container):
    manager, manifest = container.deployment.databases, database_manifest()
    production = await manager.ensure(manifest, "a" * 40)
    base = conninfo_to_dict(container.settings.database_url.get_secret_value())
    container.settings.tool_database_host = base["host"]
    container.settings.tool_database_port = int(base["port"])
    async with manager.test_scope(manifest) as env:
        schema = env["FOUNDRY_TOOL_SCHEMA"]
        async with await psycopg.AsyncConnection.connect(env["FOUNDRY_TOOL_DATABASE_URL"]) as conn:
            await conn.execute("INSERT INTO records VALUES (9)")
        assert schema != production["schema_name"]
    assert not await container.db.fetch("SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,))
    assert not await container.db.fetch("SELECT * FROM agent.database_test_scopes")
    with pytest.raises(PolicyError):
        await manager.remove_test_scope(uuid4(), production["schema_name"], production["role_name"])
    # Simulate the tracked expired scope left after worker termination.
    async with manager.test_scope(manifest):
        await container.db.execute(
            "UPDATE agent.database_test_scopes SET expires_at=now()-interval '1 second'"
        )
        await manager.reap_tests()
    assert not await container.db.fetch("SELECT * FROM agent.database_test_scopes")
    assert await manager.public(manifest.program_id) == production


@pytest.fixture
async def database_network(container):
    postgres = os.environ.get("FOUNDRY_TEST_DATABASE_CONTAINER")
    if not os.environ.get("FOUNDRY_TEST_DOCKER") or not postgres:
        pytest.skip("DB Docker tests require FOUNDRY_TEST_DOCKER and FOUNDRY_TEST_DATABASE_CONTAINER")
    settings, commands = container.settings, container.commands
    settings.docker_binary = os.environ.get("FOUNDRY_DOCKER_BINARY", "docker")
    network = "foundry-test-db-" + uuid4().hex
    settings.tool_database_network = network
    await commands.run([settings.docker_binary, "network", "create", "--internal", network])
    try:
        await commands.run(
            [settings.docker_binary, "network", "connect", "--alias", "postgres", network, postgres]
        )
        yield network
    finally:
        await commands.run([settings.docker_binary, "network", "disconnect", network, postgres])
        await commands.run([settings.docker_binary, "network", "rm", network])


@pytest.mark.docker
async def test_database_builder_validation_and_runtime(container, database_network, tmp_path):
    manifest = database_manifest()
    source = """import os
import psycopg
def run(data):
    with psycopg.connect(os.environ["FOUNDRY_TOOL_DATABASE_URL"]) as conn:
        conn.execute("INSERT INTO records VALUES (1)")
        count = conn.execute("SELECT count(*) FROM records").fetchone()[0]
        return {"count": count}
"""
    bundle = Bundle(
        manifest=manifest,
        files={
            "app/main.py": source,
            "tests/test_main.py": "from app.main import run\ndef test_persistence():\n    assert run({}) == {'count': 1}\n    assert run({}) == {'count': 2}\n",
            "README.md": "Synthetic persistent counter used to verify schema isolation.",
        },
    )
    settings, commands = container.settings, container.commands
    settings.catalog_enabled = True
    settings.tool_repository = str(tmp_path / "shared.git")
    await commands.run(["git", "init", "--bare", "--initial-branch=main", settings.tool_repository])
    root = settings.tool_repository_root
    await commands.run(["git", "clone", settings.tool_repository, str(root)])
    directory = root / "tools" / manifest.name
    directory.mkdir(parents=True)
    container.builder.write_bundle(bundle, directory)
    assert (directory / "migrations/001_tables.json").is_file()
    await commands.run(["git", "add", "."], cwd=root)
    await commands.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Database tool"],
        cwd=root,
    )
    commit = (await commands.run(["git", "rev-parse", "HEAD"], cwd=root)).decode().strip()
    await commands.run(["git", "push", "origin", "main"], cwd=root)
    assert (await container.catalog.sync())["programs"] == 1
    references = [
        {
            "id": str(manifest.program_id),
            "repository": settings.tool_repository,
            "repository_path": "tools/" + manifest.name,
            "git_commit": commit,
        }
    ]
    job_id = await container.queue.enqueue("INSTALL", str(manifest.program_id), {"references": references})
    await container.worker.process(await container.queue.claim())
    assert (await container.queue.get(job_id))["status"] == "SUCCEEDED"
    receipt = json.loads(
        (settings.state_root / "deployments" / str(manifest.program_id) / f"{commit}.json").read_text()
    )
    assert receipt["evidence"]["database_test_isolation"]
    env = await container.deployment.databases.runtime(manifest.program_id)
    # Unit tests and four sample runs inserted data only into disposable schemas.
    assert json.loads(await container.sandbox.run(receipt["image"], manifest, {}, database_env=env)) == {
        "count": 1
    }
    assert json.loads(await container.sandbox.run(receipt["image"], manifest, {}, database_env=env)) == {
        "count": 2
    }
    assert await container.executor.execute(manifest.program_id, {}, uuid4()) == {"count": 3}
    assert not await container.db.fetch("SELECT * FROM agent.database_test_scopes")
    events = await container.db.fetch("SELECT data FROM agent.events")
    assert conninfo_to_dict(env["FOUNDRY_TOOL_DATABASE_URL"])["password"] not in str(events)
    with pytest.raises(PolicyError, match="no runtime binding"):
        await container.sandbox.run(receipt["image"], manifest, {})


@pytest.mark.docker
async def test_record_store_seed_contract(container, database_network):
    directory = Path(__file__).parents[2] / "agent-tools/tools/record-store"
    if not directory.exists():
        pytest.skip("Clone sibling agent-tools to verify its DB example")
    manifest = Manifest.model_validate_json((directory / "manifest.json").read_text())
    image = await container.sandbox.build(directory, manifest)
    assert (await container.sandbox.validate(image, manifest))["passed"]
