import json
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_foundry.api import create_app
from agent_foundry.llm import LLM
from agent_foundry.profiles import ConnectionProfile, ProfileUpdate
from agent_foundry.providers import PROVIDERS, completion_request
from agent_foundry.security import PolicyError


def reply(provider, content='{"answer":"ready"}'):
    if provider == "anthropic":
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": content}],
            "usage": {"input_tokens": 12, "cache_read_input_tokens": 4, "output_tokens": 8},
        }
    if provider == "gemini":
        return {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"parts": [{"thought": True, "text": "private reasoning"}, {"text": content}]},
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 6,
                "thoughtsTokenCount": 2,
                "totalTokenCount": 20,
            },
        }
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }


@pytest.mark.parametrize(
    "provider,path,header,token_field",
    [
        ("openai", "/v1/chat/completions", "authorization", "max_completion_tokens"),
        ("anthropic", "/v1/messages", "x-api-key", "max_tokens"),
        ("gemini", "/v1/models/chosen-model:generateContent", "x-goog-api-key", "maxOutputTokens"),
        ("deepseek", "/v1/chat/completions", "authorization", "max_tokens"),
        ("groq", "/v1/chat/completions", "authorization", "max_completion_tokens"),
        ("mistral", "/v1/chat/completions", "authorization", "max_tokens"),
        ("openrouter", "/v1/chat/completions", "authorization", "max_tokens"),
        ("compatible", "/v1/chat/completions", "authorization", "max_tokens"),
    ],
)
@pytest.mark.parametrize("role", ["main", "router", "evaluator", "builder"])
async def test_each_role_uses_selected_provider_wire_format(
    settings, provider, path, header, token_field, role
):
    profile = ConnectionProfile(
        provider=provider,
        base_url="https://api.test/v1",
        api_key="test-private-key",
        **{r + "_model": "chosen-model" for r in ["main", "router", "evaluator", "builder"]},
    )
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=reply(provider))

    store, db = AsyncMock(), AsyncMock()
    store.current.return_value = profile
    llm = LLM(settings, db, httpx.AsyncClient(transport=httpx.MockTransport(respond)), store)
    try:
        assert await llm.call(role, "Return JSON", "Test data", structured=True) == {"answer": "ready"}
        (request,) = seen
        payload = json.loads(request.content)
        assert request.url.path == path and "test-private-key" not in str(request.url)
        assert request.headers[header].endswith("test-private-key")
        if header != "authorization":
            assert "authorization" not in request.headers
        if provider == "gemini":
            assert payload["generationConfig"][token_field] == getattr(settings, role + "_max_tokens")
            assert payload["generationConfig"]["responseMimeType"] == "application/json"
            assert payload["contents"][0]["parts"][0]["text"] == "Test data"
        else:
            assert payload[token_field] == getattr(settings, role + "_max_tokens")
            if provider == "anthropic":
                assert request.headers["anthropic-version"] == "2023-06-01"
                assert payload["messages"] == [{"role": "user", "content": "Test data"}]
                assert "JSON object" in payload["system"]
                assert "response_format" not in payload
            else:
                assert payload["response_format"] == {"type": "json_object"}
        event = db.event.call_args.args[1]
        assert event["provider"] == provider and event["success"]
        assert event["usage"]["total_tokens"] == (24 if provider == "anthropic" else 20)
        assert "test-private-key" not in str(event) and "private reasoning" not in str(event)
    finally:
        await llm.close()


@pytest.mark.parametrize(
    "provider,reason",
    [("anthropic", "max_tokens"), ("anthropic", "refusal"), ("gemini", "MAX_TOKENS"), ("gemini", "SAFETY")],
)
async def test_native_provider_partial_or_refused_results_do_not_succeed(settings, provider, reason):
    settings.llm_provider = provider
    data = reply(provider)
    if provider == "anthropic":
        data["stop_reason"] = reason
    else:
        data["candidates"][0]["finishReason"] = reason
    db = AsyncMock()
    llm = LLM(
        settings,
        db,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=data))),
    )
    try:
        with pytest.raises(PolicyError):
            await llm.call("builder", "JSON", "input", structured=True)
        assert not db.event.call_args.args[1]["success"]
        assert db.event.call_args.args[1]["usage"]["total_tokens"] > 0
    finally:
        await llm.close()


def test_gemini_model_cannot_change_request_path():
    profile = ConnectionProfile(provider="gemini")
    for model in ["../../other", "model?key=bad", "https://other.example/model"]:
        with pytest.raises(PolicyError):
            completion_request(profile, model, "system", "user", 100, True)


@pytest.mark.integration
@pytest.mark.parametrize("provider", ["anthropic", "gemini", "mistral"])
async def test_provider_model_discovery_headers_pagination_and_text_filter(container, provider):
    container.settings.llm_private_hosts = ["api.test"]
    calls = []

    def respond(request):
        calls.append(request)
        assert "private-discovery-key" not in str(request.url)
        if provider == "anthropic":
            assert request.headers["x-api-key"] == "private-discovery-key"
            assert request.headers["anthropic-version"] == "2023-06-01"
            if len(calls) == 1:
                return httpx.Response(
                    200, json={"data": [{"id": "model-a"}], "has_more": True, "last_id": "model-a"}
                )
            assert request.url.params["after_id"] == "model-a"
            return httpx.Response(200, json={"data": [{"id": "model-b"}], "has_more": False})
        if provider == "gemini":
            assert request.headers["x-goog-api-key"] == "private-discovery-key"
            if len(calls) == 1:
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {"name": "models/model-a", "supportedGenerationMethods": ["generateContent"]},
                            {"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]},
                        ],
                        "nextPageToken": "next",
                    },
                )
            assert request.url.params["pageToken"] == "next"
            return httpx.Response(
                200,
                json={
                    "models": [{"name": "models/model-b", "supportedGenerationMethods": ["generateContent"]}]
                },
            )
        assert request.headers["authorization"] == "Bearer private-discovery-key"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "model-a", "capabilities": {"completion_chat": True}},
                    {"id": "embedding", "capabilities": {"completion_chat": False}},
                    {"id": "model-b", "capabilities": None},
                ]
            },
        )

    profile = ProfileUpdate(
        provider=provider, base_url="https://api.test/v1", api_key="private-discovery-key"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await container.profiles.models(profile, client)
    assert result["models"] == ["model-a", "model-b"]
    assert len(calls) == (1 if provider == "mistral" else 2)
    assert not await container.db.fetch("SELECT * FROM agent.console_settings")


@pytest.mark.integration
async def test_provider_switch_does_not_reuse_key_even_on_same_proxy(container):
    container.settings.llm_private_hosts = ["api.test"]
    await container.profiles.save(
        ProfileUpdate(provider="compatible", base_url="https://api.test/v1", api_key="old-private-key")
    )
    for provider, url in [("anthropic", "https://api.test/v1"), ("compatible", "https://api.test/other")]:
        with pytest.raises(PolicyError, match="새 API 키"):
            await container.profiles.merge(ProfileUpdate(provider=provider, base_url=url))
    await container.profiles.save(
        ProfileUpdate(provider="anthropic", base_url="https://api.test/v1", api_key="new-private-key")
    )
    assert (await container.profiles.current()).api_key.get_secret_value() == "new-private-key"
    assert "new-private-key" not in str(await container.profiles.public())


@pytest.mark.integration
async def test_local_admin_receives_provider_options_and_missing_key_is_clear(container):
    container.settings.local_admin = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container.settings, container)),
        base_url="http://localhost:8000",
    ) as client:
        result = await client.get("/ui/providers")
        assert {p["id"] for p in result.json()["items"]} == set(PROVIDERS)
        result = await client.post(
            "/ui/connection/test", json={"provider": "gemini", "base_url": PROVIDERS["gemini"].base_url}
        )
        assert result.status_code == 422 and "API 키" in result.json()["detail"]
