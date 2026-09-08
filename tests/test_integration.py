import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_foundry.api import create_app
from agent_foundry.models import AgentRequest, Manifest
from agent_foundry.security import PolicyError

pytestmark = pytest.mark.integration


async def test_postgres_search_zero_token_execution(container):
    container.service.llm = AsyncMock()
    result = await container.service.respond(AgentRequest(prompt="0.1 + 0.2"))
    assert result["result"] == {"result": "0.3"} and result["route"] == "deterministic"
    container.service.llm.call.assert_not_called()
    row = await container.registry.get(result["programs"][0])
    assert row["usage_count"] == 1 and row["success_count"] == 1


async def test_fallback_queues_without_waiting_for_builder(container):
    llm = AsyncMock()
    llm.call.return_value = "설명 답변"
    container.service.llm = llm
    result = await container.service.respond(AgentRequest(prompt="인간의 창의성에 관해 설명해줘"))
    assert result["answer"] == "설명 답변"
    job = await container.queue.get(result["evaluation_job_id"])
    assert job["status"] == "PENDING" and job["kind"] == "EVALUATE"
    assert "payload" not in job
    rows = await container.db.fetch("SELECT payload FROM agent.jobs")
    assert "창의성" not in rows[0]["payload"]
    events = await container.db.fetch("SELECT data FROM agent.events")
    assert all("창의성" not in str(e) for e in events)


async def test_concurrent_dedup_and_claim(container):
    ids = await asyncio.gather(
        *[container.queue.enqueue("BUILD", "same-capability", {"capability": "demo"}) for _ in range(6)]
    )
    assert len(set(ids)) == 1
    claims = await asyncio.gather(*[container.queue.claim() for _ in range(4)])
    job = [j for j in claims if j]
    assert len(job) == 1
    await container.queue.finish(job[0], "SUCCEEDED", {"passed": True})
    assert (await container.queue.get(ids[0]))["status"] == "SUCCEEDED"


async def test_expired_job_recovery_fences_old_owner(container):
    await container.queue.enqueue("BUILD", "recover", {"capability": "demo"})
    old = await container.queue.claim()
    await container.db.execute(
        "UPDATE agent.jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (old["id"],)
    )
    new = await container.queue.claim()
    assert new["id"] == old["id"] and new["owner"] != old["owner"] and new["attempts"] == 2
    with pytest.raises(PolicyError):
        await container.queue.finish(old, "SUCCEEDED")
    await container.queue.finish(new, "FAILED", error="test failure")
    rows = await container.db.fetch("SELECT payload FROM agent.jobs WHERE id=%s", (old["id"],))
    assert rows[0]["payload"] is None


async def test_http_api_auth_boundary(container):
    app = create_app(container.settings, container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/v1/agent", json={"prompt": "1+1"})).status_code == 401
        user = {"Authorization": "Bearer test-user"}
        response = await client.post("/v1/agent", headers=user, json={"prompt": "1+1"})
        assert response.status_code == 200 and response.json()["result"]["result"] == "2"
        assert (await client.get("/admin/programs", headers=user)).status_code == 401
        result = await client.get("/v1/programs/search", params={"q": "1+1"}, headers=user)
        assert "endpoint" not in result.text and "repository" not in result.text


async def test_untested_release_cannot_be_active(container):
    m = Manifest(
        name="unverified",
        version="1.0.0",
        description="Unverified generated tool",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    with pytest.raises(PolicyError):
        await container.registry.register(m, status="ACTIVE", commit="a" * 40)


async def test_duplicate_builder_skips_llm(container):
    container.builder.llm = AsyncMock()
    status, result = await container.builder.build({"capability": "calculator arithmetic"})
    assert status == "SKIPPED"
    container.builder.llm.call.assert_not_called()


async def test_migration_is_additive_and_rejects_type_changes(container):
    m = Manifest(
        name="table-demo",
        version="1.0.0",
        description="Table definition demonstration",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        requires_db=True,
        tables=[{"name": "metrics", "columns": {"reading": "text"}, "indexes": [["reading"]]}],
    )
    await container.deployment.apply_tables(m, "a" * 40)
    await container.deployment.apply_tables(m, "a" * 40)
    m.tables[0].columns["reading"] = "integer"
    with pytest.raises(PolicyError):
        await container.deployment.apply_tables(m, "b" * 40)


async def test_worker_records_bounded_failure(container):
    await container.queue.enqueue("BUILD", "fails", {"capability": "unsupported"})
    job = await container.queue.claim()
    container.worker.builder = AsyncMock()
    container.worker.builder.build.side_effect = ValueError("password=hunter2")
    await container.worker.process(job)
    result = await container.queue.get(job["id"])
    assert result["status"] == "FAILED" and "hunter2" not in result["error"]
