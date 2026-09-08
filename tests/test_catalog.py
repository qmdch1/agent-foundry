import asyncio
import os
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from psycopg import AsyncConnection, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import SecretStr

from agent_foundry.api import create_app
from agent_foundry.container import Container
from agent_foundry.models import AgentRequest, Manifest
from agent_foundry.security import PolicyError
from agent_foundry.seed import seed
from agent_foundry.usage import UsageAccounting

pytestmark = pytest.mark.integration


async def test_local_hit_never_searches_git_catalog(container):
    container.settings.catalog_enabled = True
    container.catalog.search = AsyncMock()
    result = await container.service.respond(AgentRequest(prompt="1+1"))
    assert result["result"] == {"result": "2"}
    container.catalog.search.search.assert_not_called()


async def test_unavailable_llm_still_queues_published_tool_discovery(container):
    container.settings.catalog_enabled = True
    container.service.llm = AsyncMock()
    container.service.llm.call.side_effect = PolicyError("Model not configured")
    result = await container.service.respond(AgentRequest(prompt="unseen tool task"))
    assert result["route"] == "discovery_pending"
    assert (await container.queue.get(result["evaluation_job_id"]))["kind"] == "DISCOVER"


async def test_concurrent_registration_does_not_lose_execution_counts(container):
    row = (await container.registry.active())[0]
    manifest = Manifest.model_validate(row["manifest"])
    await asyncio.gather(
        *[
            container.registry.record_execution(row["id"], True, 5)
            if i % 2
            else container.registry.register(manifest, status="ACTIVE")
            for i in range(30)
        ]
    )
    assert (await container.registry.get(row["id"]))["usage_count"] == 15


async def test_catalog_http_metadata_install_queue_and_worker_status(container, tmp_path):
    manifest, _, _ = await publish(container, tmp_path)
    await container.catalog.sync()
    app = create_app(container.settings, container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        candidates = await client.get(
            "/v1/programs/search",
            params={"q": "Convert 0 Celsius to Fahrenheit", "source": "github"},
            headers={"Authorization": "Bearer test-user"},
        )
        assert candidates.status_code == 200 and "repository" not in candidates.text
        login = await client.post(
            "/ui/login", headers={"Origin": "http://test"}, json={"access_key": "test-admin"}
        )
        client.headers.update({"Origin": "http://test", "X-Foundry-CSRF": login.json()["csrf_token"]})
        catalog = (await client.get("/ui/catalog")).json()
        assert catalog["total"] == 1 and catalog["items"][0]["installed_at"] is None
        install = await client.post(f"/ui/catalog/{manifest.program_id}/install")
        job = (await client.get("/ui/jobs/" + install.json()["job_id"])).json()
        assert job["kind"] == "INSTALL" and job["status"] == "PENDING"
        assert "payload" not in job and "result" not in job
        assert len((await client.get("/ui/activity")).json()) == 1


async def publish(container, tmp_path):
    settings = container.settings
    settings.catalog_enabled = True
    settings.tool_repository = str(tmp_path / "shared.git")
    root, commands = settings.tool_repository_root, container.commands
    await commands.run(["git", "init", "--bare", "--initial-branch=main", settings.tool_repository])
    await commands.run(["git", "clone", settings.tool_repository, str(root)])
    tool = root / "tools/temperature-converter"
    (tool / "app").mkdir(parents=True)
    (tool / "tests").mkdir()
    (tool / "generation_tokens.txt").write_text("246\n")
    manifest = Manifest(
        name="temperature-converter",
        version="1.0.0",
        description="Celsius Fahrenheit temperature conversion 섭씨 화씨 온도 변환",
        input_schema={
            "type": "object",
            "properties": {"celsius": {"type": "number"}},
            "required": ["celsius"],
        },
        output_schema={
            "type": "object",
            "properties": {"fahrenheit": {"type": "number"}},
            "required": ["fahrenheit"],
        },
        examples=[
            {
                "prompt": "Convert 0 Celsius to Fahrenheit",
                "input": {"celsius": 0},
                "output": {"fahrenheit": 32},
            }
        ],
    )
    (tool / "manifest.json").write_text(manifest.model_dump_json())
    (tool / "app/main.py").write_text("def run(data):\n    return {'fahrenheit':data['celsius']*9/5+32}\n")
    (tool / "tests/test_main.py").write_text(
        "from app.main import run\ndef test_conversion():\n    assert run({'celsius':0})=={'fahrenheit':32}\n"
    )
    (tool / "requirements.txt").write_text("")
    await commands.run(["git", "add", "."], cwd=root)
    await commands.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@localhost",
            "commit",
            "-m",
            "Publish reusable conversion",
        ],
        cwd=root,
    )
    commit = (await commands.run(["git", "rev-parse", "HEAD"], cwd=root)).decode().strip()
    await commands.run(["git", "push", "origin", "main"], cwd=root)
    return manifest, commit, root


async def test_catalog_indexes_only_pushed_sources_and_preserves_immutable_tool_revision(container, tmp_path):
    manifest, commit, root = await publish(container, tmp_path)
    assert (await container.catalog.sync())["programs"] == 1
    assert (await container.db.fetch("SELECT creation_tokens FROM agent.catalog"))[0][
        "creation_tokens"
    ] == 246
    candidates = await container.catalog.search.search("Convert 0 Celsius to Fahrenheit")
    assert candidates[0].program_id == manifest.program_id and candidates[0].mapped_input == {"celsius": 0}
    assert not await container.db.fetch("SELECT id FROM agent.programs WHERE id=%s", (manifest.program_id,))
    (root / "README.md").write_text("This unpushed file is not part of the catalog")
    assert not (await container.catalog.sync())["changed"]
    await container.commands.run(["git", "add", "README.md"], cwd=root)
    await container.commands.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Documentation"],
        cwd=root,
    )
    await container.commands.run(["git", "push", "origin", "main"], cwd=root)
    await container.catalog.sync()
    rows = await container.db.fetch("SELECT git_commit FROM agent.catalog")
    assert rows[0]["git_commit"] == commit


async def test_api_queues_remote_install_without_running_deployment_or_main_llm(container, tmp_path):
    await publish(container, tmp_path)
    await container.catalog.sync()
    container.service.llm = AsyncMock()
    container.catalog.install = AsyncMock()
    response = await container.service.respond(
        AgentRequest(prompt="Convert 0 Celsius to Fahrenheit", allow_build=False)
    )
    assert response["route"] == "installation_pending" and response["result"] is None
    job = await container.queue.get(response["installation_job_id"])
    assert job["kind"] == "INSTALL" and job["status"] == "PENDING"
    container.service.llm.call.assert_not_called()
    container.catalog.install.assert_not_called()


async def test_worker_uses_existing_publication_before_evaluation_or_generation(container, tmp_path):
    await publish(container, tmp_path)
    container.catalog.install = AsyncMock(return_value=("SUCCEEDED", {"reused": True}))
    container.evaluator.llm = AsyncMock()
    status, result = await container.evaluator.evaluate({"prompt": "Convert 0 Celsius to Fahrenheit"})
    assert status == "SUCCEEDED" and result["reused"]
    container.evaluator.llm.call.assert_not_called()
    container.catalog.install.assert_awaited_once()
    container.builder.llm = AsyncMock()
    status, result = await container.builder.build({"capability": "Convert 0 Celsius to Fahrenheit"})
    assert status == "SUCCEEDED"
    container.builder.llm.call.assert_not_called()


async def test_failed_git_refresh_blocks_new_generation_and_keeps_catalog(container, tmp_path):
    await publish(container, tmp_path)
    await container.catalog.sync()
    container.catalog.commands = AsyncMock()
    container.catalog.commands.run.side_effect = PolicyError("Git unavailable")
    container.builder.llm = AsyncMock()
    with pytest.raises(PolicyError):
        await container.builder.build({"capability": "Brand new unseen capability"})
    container.builder.llm.call.assert_not_called()
    assert (await container.db.fetch("SELECT count(*) AS n FROM agent.catalog"))[0]["n"] == 1


async def test_usage_preserves_install_date_and_statistics_on_reinstall(container):
    request = AgentRequest(prompt="0.1 + 0.2")
    first = await container.service.respond(request)
    row = await container.registry.get(first["programs"][0])
    assert row["installed_at"] and row["estimated_tokens_saved"] > 0
    assert first["usage"]["actual_llm_tokens"] == 0
    await container.registry.register(Manifest.model_validate(row["manifest"]), status="ACTIVE")
    restored = await container.registry.get(row["id"])
    assert restored["installed_at"] == row["installed_at"]
    assert (
        restored["usage_count"] == 1 and restored["estimated_tokens_saved"] == row["estimated_tokens_saved"]
    )


async def test_savings_uses_observed_baseline_subtracts_llm_and_is_idempotent(container):
    program = (await container.registry.active())[0]
    rid = uuid4()
    await container.db.execute(
        "INSERT INTO agent.prompt_baselines(prompt_hash,model,total_tokens) VALUES ('synthetic','test',800)"
    )
    await container.db.event("llm", {"role": "router", "model": "test", "usage": {"total_tokens": 75}}, rid)
    usage = UsageAccounting(container.db, container.settings)
    result = await usage.record(rid, "synthetic", "A request", "router", [str(program["id"])], {"result": 1})
    assert result["estimated_tokens_saved"] == 725 and result["baseline_method"] == "previous_llm_response"
    await usage.record(rid, "synthetic", "A request", "router", [str(program["id"])], {"result": 1})
    row = await container.registry.get(program["id"])
    assert row["estimated_tokens_saved"] == 725 and row["attributed_llm_tokens"] == 75
    unknown = uuid4()
    await container.db.event("llm", {"role": "main", "usage": None}, unknown)
    result = await usage.record(unknown, "other", "Another request", "router", [str(program["id"])], {})
    assert result["estimated_tokens_saved"] is None


@pytest.mark.docker
async def test_fresh_second_platform_discovers_installs_and_executes_from_git(container, tmp_path):
    if not os.environ.get("FOUNDRY_TEST_DOCKER"):
        pytest.skip("Set FOUNDRY_TEST_DOCKER=1")
    manifest, commit, _ = await publish(container, tmp_path)
    config = container.settings.model_copy(deep=True)
    config.docker_binary = os.environ.get("FOUNDRY_DOCKER_BINARY", "docker")
    config.state_root = tmp_path / "second-platform"
    config.tool_repository_root = tmp_path / "second-checkout"
    # A separate empty database proves this does not depend on the first platform's Registry or backups.
    connection = conninfo_to_dict(config.database_url.get_secret_value())
    database = "foundry_test_peer_" + uuid4().hex[:12]
    admin = await AsyncConnection.connect(config.database_url.get_secret_value(), autocommit=True)
    await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    connection["dbname"] = database
    config.database_url = SecretStr(make_conninfo(**connection))
    peer = Container(config)
    try:
        await peer.open()
        await peer.db.migrate()
        await seed(peer.registry)
        await peer.catalog.sync()
        response = await peer.service.respond(AgentRequest(prompt="Convert 0 Celsius to Fahrenheit"))
        job = await peer.queue.claim()
        assert str(job["id"]) == response["installation_job_id"]
        await peer.worker.process(job)
        assert (await peer.queue.get(job["id"]))["status"] == "SUCCEEDED"
        result = await peer.service.respond(AgentRequest(prompt="Convert 0 Celsius to Fahrenheit"))
        assert result["result"] == {"fahrenheit": 32} and result["route"] == "deterministic"
        row = await peer.registry.get(manifest.program_id)
        assert row["git_commit"] == commit and row["installed_at"] and row["usage_count"] == 1
        assert not await container.db.fetch(
            "SELECT id FROM agent.programs WHERE id=%s", (manifest.program_id,)
        )
    finally:
        await peer.close()
        await admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
        await admin.close()
