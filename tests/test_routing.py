from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_foundry.models import Candidate, Plan
from agent_foundry.router import Router, validate_references
from agent_foundry.search import map_input
from agent_foundry.security import PolicyError
from agent_foundry.seed import seed_manifests


async def test_named_tool_prefix_finds_unseen_input_without_llm(container):
    from agent_foundry.models import Manifest

    manifest = Manifest(
        name="lookup-tool",
        description="Lookup a supplied opaque identifier deterministically.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        version="1.0.0",
        selection_rules=[{"pattern": "^lookup-tool (?P<value>[A-Z0-9]+)$", "fields": {"value": "string"}}],
    )
    await container.registry.register(manifest, status="ACTIVE", commit="b" * 40, evidence={"passed": True})
    candidates = await container.search.search("LOOKUP-TOOL X928372993877")
    assert candidates[0].program_id == manifest.program_id
    assert candidates[0].mapped_input == {"value": "X928372993877"}
    plan, route = await container.router.route("LOOKUP-TOOL X928372993877", candidates, uuid4())
    assert route == "deterministic" and plan.action == "execute"
    assert all(
        c.mapped_input is None for c in await container.search.search("lookup-tool-not-found X928372993877")
    )


def candidate(score=1, mapped=True):
    m = seed_manifests()[0]
    return Candidate(
        program_id=m.program_id,
        name=m.name,
        description=m.description,
        input_schema=m.input_schema,
        output_schema=m.output_schema,
        tags=m.tags,
        examples=m.examples,
        score=score,
        mapped_input={"expression": "0.1 + 0.2"} if mapped else None,
    )


@pytest.mark.parametrize("prompt", ["0.1 + 0.2", "계산해줘: 0.1 + 0.2", "0.1 + 0.2 계산해줘"])
def test_deterministic_mapping(prompt):
    assert map_input(prompt, seed_manifests()[0])["expression"].strip() == "0.1 + 0.2"


@pytest.mark.parametrize("prompt", ["내일 날씨", "__import__('os').system('id')", "1 + 2 그리고 파일 삭제"])
def test_no_unrelated_mapping(prompt):
    assert map_input(prompt, seed_manifests()[0]) is None


async def test_direct_selection_uses_zero_llm_calls(settings):
    llm = AsyncMock()
    plan, route = await Router(llm, settings).route("0.1 + 0.2", [candidate()], uuid4())
    assert route == "deterministic" and plan.action == "execute"
    llm.call.assert_not_called()


async def test_no_candidates_uses_zero_router_calls(settings):
    llm = AsyncMock()
    plan, _ = await Router(llm, settings).route("hello", [], uuid4())
    assert plan.action == "none"
    llm.call.assert_not_called()


async def test_unknown_program_is_rejected(settings):
    llm = AsyncMock()
    llm.call.return_value = {
        "action": "execute",
        "confidence": 1,
        "programs": [{"program_id": str(uuid4()), "order": 1, "input": {}}],
    }
    plan, route = await Router(llm, settings).route("hi", [candidate(0.5, False)], uuid4())
    assert plan.action == "none" and route == "invalid_router_response"


async def test_tie_does_not_bypass_router(settings):
    llm = AsyncMock()
    llm.call.return_value = {"action": "none", "confidence": 1, "programs": []}
    await Router(llm, settings).route("hello", [candidate(), candidate()], uuid4())
    llm.call.assert_awaited_once()
    user_payload = llm.call.call_args.args[2]
    assert "endpoint" not in user_payload and "repository" not in user_payload


def test_forward_reference_and_bad_plan_order_rejected():
    with pytest.raises(PolicyError):
        validate_references({"x": {"$from_step": 2, "path": []}}, 1)
    with pytest.raises(ValueError):
        Plan(action="execute", programs=[{"program_id": uuid4(), "order": 2}], confidence=1)
