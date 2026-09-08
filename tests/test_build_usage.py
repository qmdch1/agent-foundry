from uuid import uuid4

import httpx
import pytest

from agent_foundry.api import create_app
from agent_foundry.generation_tokens import FILENAME, add_tokens, parse_total, read_total
from agent_foundry.security import PolicyError
from agent_foundry.seed import seed_manifests
from agent_foundry.usage import UsageAccounting


@pytest.mark.parametrize("values,expected", [([120, 80], 200), ([120, None], None), ([0], 0), ([], None)])
@pytest.mark.parametrize("estimated", [False, True])
async def test_build_usage_retry_totals_and_unknown(container, tmp_path, values, expected, estimated):
    build_id = uuid4()
    manifest = seed_manifests()[0]
    manifest.generation_tokens_estimated = estimated
    commit = "a" * 40
    await container.registry.register(manifest, status="ACTIVE", commit=commit)
    for i, value in enumerate(values):
        await container.db.event(
            "llm", {"role": "builder", "success": i > 0, "usage": {"total_tokens": value}}, build_id
        )
    await container.db.event("llm", {"role": "main", "usage": {"total_tokens": 999}}, build_id)
    await container.db.event("llm", {"role": "builder", "usage": {"total_tokens": 888}}, uuid4())
    await UsageAccounting(container.db, container.settings).record_build(
        manifest.program_id, commit, build_id
    )
    records = await container.db.fetch("SELECT data FROM agent.events WHERE event_type='program_build_usage'")
    assert records[0]["data"]["total_tokens"] == expected
    assert records[0]["data"]["llm_calls"] == len(values)
    add_tokens(tmp_path, records[0]["data"]["total_tokens"], initial=True)
    await container.registry.register(
        manifest, status="ACTIVE", commit=commit, creation_tokens=read_total(tmp_path)
    )
    container.settings.local_admin = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container.settings, container)),
        base_url="http://localhost",
    ) as client:
        rows = (await client.get("/ui/programs")).json()["items"]
        assert next(p for p in rows if p["id"] == str(manifest.program_id))["creation_tokens"] == expected
        assert (
            next(p for p in rows if p["id"] == str(manifest.program_id))["creation_tokens_estimated"]
            is estimated
        )
        await container.registry.register(manifest, status="ACTIVE", commit="b" * 40)
        rows = (await client.get("/ui/programs")).json()["items"]
        assert next(p for p in rows if p["id"] == str(manifest.program_id))["creation_tokens"] is None


def test_only_final_cumulative_value_is_saved(tmp_path):
    assert add_tokens(tmp_path, 120, initial=True) == 120
    assert add_tokens(tmp_path, 80) == 200
    assert add_tokens(tmp_path, 15) == 215
    assert (tmp_path / FILENAME).read_text() == "215\n"
    with pytest.raises(PolicyError):
        add_tokens(tmp_path, 999, initial=True)
    assert read_total(tmp_path) == 215


def test_unknown_history_never_becomes_a_partial_total(tmp_path):
    assert add_tokens(tmp_path, 10) is None
    assert (tmp_path / FILENAME).read_text() == "미집계\n"
    assert add_tokens(tmp_path, 20) is None


@pytest.mark.parametrize("raw", [b"-1", b"1.5", b"10\n20", b"123 tokens", b"9" * 65])
def test_invalid_totals_rejected(raw):
    with pytest.raises(PolicyError):
        parse_total(raw)


async def test_deployment_reads_final_file_and_preserves_execution_counts(container, tmp_path):
    from unittest.mock import AsyncMock

    from agent_foundry.models import Manifest

    manifest = Manifest(
        name="token-tool",
        version="1.0.0",
        description="Creation token import test",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    container.sandbox.build = AsyncMock(return_value="sha256:" + "a" * 64)
    container.sandbox.validate = AsyncMock(return_value={"passed": True})
    add_tokens(tmp_path, 200, initial=True)
    await container.deployment.deploy(manifest, tmp_path, "a" * 40)
    await container.registry.record_execution(manifest.program_id, True, 10)
    add_tokens(tmp_path, 50)
    await container.deployment.deploy(manifest, tmp_path, "b" * 40)
    row = await container.registry.get(manifest.program_id)
    assert row["creation_tokens"] == 250 and row["usage_count"] == 1
    assert (tmp_path / FILENAME).read_text() == "250\n"
