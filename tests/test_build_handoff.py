import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from agent_foundry.api import create_app
from agent_foundry.build_handoff import MAIN_HANDOFF_SYSTEM, review_payload, unpack_answer
from agent_foundry.models import AgentRequest, BuildSpec, Candidate, Manifest
from agent_foundry.security import PolicyError


def specification():
    return {
        "objective": "Aggregate synthetic numeric records by category",
        "inputs": ["records: numeric values with optional category"],
        "outputs": ["sum and count per category"],
        "steps": ["validate finite numbers", "group and add decimal values"],
        "acceptance_checks": ["sums are invariant to input record order"],
        "requires_db": False,
        "template": "aggregation",
    }


@pytest.mark.parametrize("serialized", [False, True])
def test_main_response_unpacks_structured_answer_and_spec(serialized):
    data = {"answer": "Complete answer", "build_spec": specification()}
    answer, spec = unpack_answer(json.dumps(data) if serialized else data)
    assert answer == "Complete answer"
    assert spec == BuildSpec.model_validate(specification())


@pytest.mark.parametrize(
    "invalid",
    [{"template": "unknown"}, "invalid", 8, ["invalid"], {**specification(), "inputs": ["x" * 301]}],
)
def test_optional_invalid_spec_preserves_user_answer(invalid):
    answer, spec = unpack_answer({"answer": "The requested answer survives.", "build_spec": invalid})
    assert answer == "The requested answer survives." and spec is None


def test_non_structured_legacy_answer_survives():
    assert unpack_answer("A plain text response.") == ("A plain text response.", None)


def test_review_context_is_masked_and_bounded():
    payload = review_payload(
        "user private input",
        "password=private-value " + "A" * 1000,
        BuildSpec.model_validate(specification()),
        uuid4(),
        500,
        "Bearer private-token " + "B" * 1000,
    )
    assert len(payload["answer_context"]) == len(payload["reference_material"]) == 500
    assert "private-value" not in payload["answer_context"]
    assert "private-token" not in payload["reference_material"]
    assert payload["build_spec"]["template"] == "aggregation"


async def test_answer_and_spec_use_one_main_call_then_durable_encrypted_queue(container):
    llm = AsyncMock()
    llm.call.return_value = {"answer": "Synthetic private result.", "build_spec": specification()}
    container.service.llm = llm
    container.evaluator.evaluate = AsyncMock(side_effect=AssertionError("Request waited for evaluator"))
    container.builder.build = AsyncMock(side_effect=AssertionError("Request waited for builder"))
    prompt = "Explain this invented zenith-quartz observation process."
    result = await container.service.respond(AgentRequest(prompt=prompt))
    assert result["answer"] == "Synthetic private result."
    llm.call.assert_awaited_once()
    call = llm.call.await_args
    assert call.args == ("main", MAIN_HANDOFF_SYSTEM, prompt)
    assert call.kwargs["structured"] is True
    container.evaluator.evaluate.assert_not_awaited()
    container.builder.build.assert_not_awaited()
    job = (await container.db.fetch("SELECT * FROM agent.jobs WHERE id=%s", (result["evaluation_job_id"],)))[
        0
    ]
    assert job["kind"] == "EVALUATE" and job["status"] == "PENDING"
    assert prompt not in job["payload"] and "Synthetic private result" not in job["payload"]
    payload = container.queue.payload(job)
    assert payload["prompt"] == prompt
    assert payload["answer_context"] == result["answer"]
    assert payload["build_spec"] == specification()
    assert payload["request_id"] == result["request_id"]
    events = await container.db.fetch("SELECT data FROM agent.events")
    assert all(prompt not in str(event) and "Synthetic private result" not in str(event) for event in events)


async def test_invalid_optional_spec_does_not_make_agent_request_fail(container):
    container.service.llm = AsyncMock()
    container.service.llm.call.return_value = {"answer": "User answer preserved", "build_spec": {"bad": 1}}
    result = await container.service.respond(AgentRequest(prompt="Explain the invented azure-fern process."))
    assert result["answer"] == "User answer preserved"
    job = await container.queue.claim()
    assert container.queue.payload(job)["build_spec"] is None


@pytest.mark.parametrize("disable", ["allow_build", "builder_enabled", "main_build_spec_enabled"])
async def test_disabled_handoff_uses_plain_main_answer(container, disable):
    request = AgentRequest(prompt="Explain this invented cedar-rain observation.")
    if disable == "allow_build":
        request.allow_build = False
    else:
        setattr(container.settings, disable, False)
    container.service.llm = AsyncMock()
    container.service.llm.call.return_value = "Plain answer"
    result = await container.service.respond(request)
    assert result["answer"] == "Plain answer"
    assert container.service.llm.call.await_args.kwargs["structured"] is False
    if disable != "main_build_spec_enabled":
        assert result["evaluation_job_id"] is None


async def test_external_review_api_returns_202_without_calling_llm_or_builder(container):
    container.service.llm = AsyncMock(side_effect=AssertionError("Unexpected LLM"))
    container.evaluator.evaluate = AsyncMock(side_effect=AssertionError("Unexpected evaluation"))
    container.builder.build = AsyncMock(side_effect=AssertionError("Unexpected build"))
    body = {
        "prompt": "external private prompt",
        "answer": "external private answer",
        "reference_material": "source facts already researched",
        "build_spec": specification(),
    }
    app = create_app(container.settings, container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await asyncio.wait_for(
            client.post("/v1/build-reviews", json=body, headers={"Authorization": "Bearer test-user"}), 5
        )
        assert response.status_code == 202
        result = response.json()
        assert result["status"] == "queued"
        # Admin status never leaks the encrypted request payload back through the API.
        status = await client.get(
            f"/admin/jobs/{result['evaluation_job_id']}", headers={"Authorization": "Bearer test-admin"}
        )
        assert status.status_code == 200 and "payload" not in status.json()
    assert not container.service.llm.mock_calls
    container.evaluator.evaluate.assert_not_awaited()
    container.builder.build.assert_not_awaited()
    job = await container.queue.claim()
    assert job["kind"] == "EVALUATE"
    assert "external private" not in job["payload"]
    payload = container.queue.payload(job)
    assert payload["answer_context"] == body["answer"]
    assert payload["reference_material"] == body["reference_material"]
    assert payload["build_spec"] == specification()


async def test_external_review_api_auth_validation_and_disabled_builder(container):
    app = create_app(container.settings, container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"prompt": "Review synthetic task"}
        assert (await client.post("/v1/build-reviews", json=body)).status_code == 401
        assert (
            await client.post("/v1/build-reviews", json=body, headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        user = {"Authorization": "Bearer test-user"}
        assert (await client.post("/v1/build-reviews", json={"prompt": ""}, headers=user)).status_code == 422
        assert (
            await client.post("/v1/build-reviews", json={**body, "build_spec": {"bad": 1}}, headers=user)
        ).status_code == 422
        container.settings.builder_enabled = False
        assert (await client.post("/v1/build-reviews", json=body, headers=user)).status_code == 409
    assert not await container.db.fetch("SELECT id FROM agent.jobs")


async def extension_candidate(container, repository=None):
    manifest = Manifest(
        name="synthetic-aggregate",
        version="1.0.0",
        description="Aggregate synthetic numeric records",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
    )
    await container.registry.register(
        manifest,
        status="ACTIVE",
        repository=repository or container.settings.tool_repository,
        path="tools/synthetic-aggregate",
        commit="a" * 40,
        evidence={"passed": True},
    )
    candidate = Candidate(
        program_id=manifest.program_id,
        name=manifest.name,
        description=manifest.description,
        input_schema=manifest.input_schema,
        output_schema=manifest.output_schema,
        tags=[],
        examples=[],
        score=0.6,
    )
    container.evaluator.search = AsyncMock()
    container.evaluator.search.search.return_value = [candidate]
    return candidate


def extension_evaluation(program_id, strategy="extend"):
    return {
        "reuse_score": 1,
        "determinism_score": 1,
        "token_saving_score": 1,
        "latency_saving_score": 1,
        "accuracy_gain_score": 1,
        "specialized_data_score": 1,
        "maintenance_cost": 0,
        "estimated_saved_tokens_per_use": 100000,
        "capability": "Add grouped numeric aggregation",
        "reason": "Synthetic recurring workload",
        "build_spec": specification(),
        "strategy": strategy,
        "target_program_id": str(program_id),
    }


async def test_evaluator_extension_binds_known_revision_and_serializes_uuid_in_build_job(container):
    candidate = await extension_candidate(container)
    container.evaluator.llm = AsyncMock()
    container.evaluator.llm.call.return_value = extension_evaluation(candidate.program_id)
    request_id = str(uuid4())
    payload = {
        "prompt": "Synthetic grouping request",
        "answer_context": "Already explained algorithm",
        "reference_material": "Synthetic source field definitions",
        "build_spec": specification(),
        "request_id": request_id,
    }
    status, decision = await container.evaluator.evaluate(payload)
    assert status == "SUCCEEDED" and decision["approved"]
    call_data = json.loads(container.evaluator.llm.call.await_args.args[2])
    assert call_data["answer_context"] == payload["answer_context"]
    assert call_data["reference_material"] == payload["reference_material"]
    assert call_data["initial_build_spec"] == specification()
    assert call_data["existing_candidates"][0]["program_id"] == str(candidate.program_id)
    job = await container.queue.claim()
    assert job["kind"] == "BUILD" and str(job["id"]) == decision["build_job_id"]
    build = container.queue.payload(job)
    assert build["extension"] == {"program_id": str(candidate.program_id), "git_commit": "a" * 40}
    assert build["evaluation"]["target_program_id"] == str(candidate.program_id)
    assert build["request_id"] == request_id and build["build_spec"] == specification()
    # Only the evaluator's generic refined spec crosses into generation; raw sources stay out.
    assert "answer_context" not in build and "reference_material" not in build and "prompt" not in build


@pytest.mark.parametrize("strategy", ["extend", "reuse"])
async def test_evaluator_rejects_target_absent_from_search_candidates(container, strategy):
    await extension_candidate(container)
    container.evaluator.llm = AsyncMock()
    container.evaluator.llm.call.return_value = extension_evaluation(uuid4(), strategy)
    with pytest.raises(PolicyError, match="unknown"):
        await container.evaluator.evaluate({"prompt": "Synthetic grouping request"})
    assert not await container.db.fetch("SELECT id FROM agent.jobs WHERE kind='BUILD'")


async def test_evaluator_rejects_known_target_outside_approved_repository(container):
    candidate = await extension_candidate(container, "https://example.invalid/unapproved.git")
    container.evaluator.llm = AsyncMock()
    container.evaluator.llm.call.return_value = extension_evaluation(candidate.program_id)
    with pytest.raises(PolicyError, match="approved repository"):
        await container.evaluator.evaluate({"prompt": "Synthetic grouping request"})
    assert not await container.db.fetch("SELECT id FROM agent.jobs WHERE kind='BUILD'")
