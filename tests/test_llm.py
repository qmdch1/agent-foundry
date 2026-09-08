import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from agent_foundry.llm import LLM
from agent_foundry.security import PolicyError


async def test_compatible_api_json_and_usage(settings):
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"action":"none","programs":[],"confidence":1}'},
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            },
        )

    db = AsyncMock()
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    llm = LLM(settings, db, client)
    try:
        result = await llm.call("router", "JSON only", "hello", structured=True, request_id=uuid4())
        assert result["action"] == "none"
        assert seen[0]["model"] == "test-router" and seen[0]["response_format"]["type"] == "json_object"
        event = db.event.call_args.args[1]
        assert event["usage"]["total_tokens"] == 20 and event["success"]
    finally:
        await llm.close()


@pytest.mark.parametrize("finish,refusal", [("length", None), ("stop", "refused")])
async def test_incomplete_or_refused_llm_response_fails_closed(settings, finish, refusal):
    db = AsyncMock()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": finish, "message": {"content": "{}", "refusal": refusal}}]
                },
            )
        )
    )
    llm = LLM(settings, db, client)
    try:
        with pytest.raises(PolicyError):
            await llm.call("builder", "JSON", "test", structured=True)
        assert not db.event.call_args.args[1]["success"]
    finally:
        await llm.close()
