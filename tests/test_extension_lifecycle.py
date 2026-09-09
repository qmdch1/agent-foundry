import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_program_databases import database_network  # noqa: F401

from agent_foundry.models import Bundle, Manifest
from agent_foundry.security import PolicyError

pytestmark = [pytest.mark.integration, pytest.mark.docker]


@pytest.mark.usefixtures("database_network")
@pytest.mark.parametrize("publish", [True, False])
async def test_builder_extension_preserves_data_tests_and_cumulative_usage(container, tmp_path, publish):
    settings, commands = container.settings, container.commands
    settings.auto_extension_enabled = True
    settings.git_push = publish
    settings.local_releases_enabled = not publish
    settings.tool_repository = str(tmp_path / "extension.git")
    root = settings.tool_repository_root
    await commands.run(["git", "init", "--bare", "--initial-branch=main", settings.tool_repository])
    await commands.run(["git", "clone", settings.tool_repository, str(root)])
    manifest = Manifest(
        name="extension-counter-" + uuid4().hex[:8],
        version="1.0.0",
        description="Synthetic persistent tally for upgrade validation",
        requires_db=True,
        network="database",
        tables=[{"name": "records", "columns": {"value": "integer"}}],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        examples=[{"prompt": "Increment synthetic tally", "input": {}, "output": {"count": 1}}],
        generation_tokens_estimated=True,
    )
    source = """import os
import psycopg
def run(data):
    with psycopg.connect(os.environ["FOUNDRY_TOOL_DATABASE_URL"]) as conn:
        conn.execute("INSERT INTO records(value) VALUES (1)")
        count = conn.execute("SELECT count(*) FROM records").fetchone()[0]
    return {"count": count}
"""
    original_tests = """from app.main import run
def test_original_increment():
    assert run({}) == {"count": 1}
    assert run({}) == {"count": 2}
"""
    files = {"app/main.py": source, "tests/test_main.py": original_tests, "README.md": "Synthetic tally."}
    directory = root / "tools" / manifest.name
    directory.mkdir(parents=True)
    container.builder.write_bundle(Bundle(manifest=manifest, files=files), directory)
    (directory / "generation_tokens.txt").write_text("120\n")
    await commands.run(["git", "add", "."], cwd=root)
    await commands.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Base tally"],
        cwd=root,
    )
    original_commit = (await commands.run(["git", "rev-parse", "HEAD"], cwd=root)).decode().strip()
    await commands.run(["git", "push", "origin", "main"], cwd=root)
    checked_out = await container.deployment.checkout(
        settings.tool_repository, original_commit, f"tools/{manifest.name}"
    )
    await container.deployment.deploy(manifest, checked_out, original_commit)
    assert await container.executor.execute(manifest.program_id, {}, uuid4()) == {"count": 1}
    before = await container.registry.get(manifest.program_id)
    database_before = await container.deployment.databases.public(manifest.program_id)

    new_manifest = manifest.model_copy(deep=True)
    new_manifest.version = "1.1.0"
    new_manifest.examples = []  # The platform must restore original regression samples.
    new_source = source.replace(
        '        conn.execute("INSERT INTO records(value) VALUES (1)")',
        '        if data.get("operation") != "read":\n'
        '            conn.execute("INSERT INTO records(value) VALUES (1)")',
    )
    new_test = """from app.main import run
def test_new_read_does_not_insert():
    first = run({"operation": "read"})
    assert isinstance(first["count"], int)
    assert run({"operation": "read"}) == first
"""
    generated = {
        "manifest": new_manifest.model_dump(mode="json"),
        "files": {"app/main.py": new_source, "tests/test_main.py": new_test, "README.md": "Tally and read."},
    }

    async def fake_builder_call(role, system, user, **kwargs):
        assert role == "builder"
        supplied = json.loads(user)
        assert supplied["existing_files"]["tests/test_main.py"] == original_tests
        assert supplied["existing_manifest"]["version"] == "1.0.0"
        await container.db.event(
            "llm",
            {"role": "builder", "success": True, "usage": {"total_tokens": 80}},
            kwargs["request_id"],
        )
        return generated

    container.builder.llm = AsyncMock()
    container.builder.llm.call.side_effect = fake_builder_call
    payload = {
        "capability": "Read tally without incrementing the existing counter",
        "extension": {"program_id": str(manifest.program_id), "git_commit": original_commit},
    }
    status, result = await container.builder.build(payload)
    assert status == "SUCCEEDED" and result["status"] == "ACTIVE"
    assert result["version"] == "1.1.0" and result["git_commit"] != original_commit
    assert (directory / "tests/test_main.py").read_text() == original_tests
    assert any(p.read_text() == new_test for p in (directory / "tests").glob("test_extension_*.py"))
    assert (directory / "generation_tokens.txt").read_text() == "200\n"
    published_manifest = Manifest.model_validate_json((directory / "manifest.json").read_text())
    assert published_manifest.examples == manifest.examples
    assert published_manifest.generation_tokens_estimated
    remote_commit = (await commands.run(["git", "ls-remote", "origin", "refs/heads/main"], cwd=root)).decode()
    assert remote_commit.startswith(result["git_commit"] if publish else original_commit)
    assert result["published"] is publish
    if not publish:
        assert all(r["success"] for r in await container.deployment.reconcile())

    after = await container.registry.get(manifest.program_id)
    assert after["creation_tokens"] == 200
    assert after["usage_count"] == before["usage_count"]
    assert after["created_at"] == before["created_at"]
    database_after = await container.deployment.databases.public(manifest.program_id)
    assert database_after["schema_name"] == database_before["schema_name"]
    assert database_after["created_at"] == database_before["created_at"]
    assert await container.executor.execute(manifest.program_id, {"operation": "read"}, uuid4()) == {
        "count": 1
    }
    assert await container.executor.execute(manifest.program_id, {}, uuid4()) == {"count": 2}
    assert not await container.db.fetch("SELECT * FROM agent.database_test_scopes")
    with pytest.raises(PolicyError, match="reviewed active revision"):
        await container.builder.build(payload)
    assert container.builder.llm.call.call_count == 1
