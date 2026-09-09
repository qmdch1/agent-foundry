import json

import httpx
import pytest

from agent_foundry.api import create_app
from agent_foundry.llm import LLM
from agent_foundry.profiles import ProfileStore, ProfileUpdate
from agent_foundry.security import PolicyError

pytestmark = pytest.mark.integration


def client_for(container):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container.settings, container)),
        base_url="http://test",
        headers={"Origin": "http://test"},
    )


async def sign_in(client, key="test-admin"):
    response = await client.post("/ui/login", json={"access_key": key})
    assert response.status_code == 200
    client.headers["X-Foundry-CSRF"] = response.json()["csrf_token"]
    return response


def update(**values):
    return ProfileUpdate(
        provider="compatible",
        base_url="https://api.test/v1",
        main_model="new-main",
        router_model="new-router",
        evaluator_model="new-evaluator",
        builder_model="new-builder",
        **values,
    )


async def test_web_root_static_assets_and_anonymous_boundary(container):
    async with client_for(container) as client:
        page = await client.get("/")
        assert page.status_code == 200 and "무엇을 도와드릴까요?" in page.text
        assert page.headers["content-security-policy"].startswith("default-src 'self'")
        for path in ("/assets/app.css", "/assets/app.js", "/assets/favicon.svg"):
            assert (await client.get(path)).status_code == 200
        assert (await client.get("/ui/programs")).status_code == 401
        assert (await client.get("/ui/settings")).status_code == 401
        assert (await client.get("/ui/session")).json() == {"authenticated": False}


async def test_cookie_auth_executes_prompt_and_enforces_csrf(container):
    async with client_for(container) as client:
        response = await sign_in(client)
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        result = await client.post("/v1/agent", json={"prompt": "0.1 + 0.2"})
        assert result.status_code == 200 and result.json()["result"]["result"] == "0.3"
        client.headers.pop("X-Foundry-CSRF")
        assert (await client.post("/v1/agent", json={"prompt": "1+1"})).status_code == 403


async def test_cross_origin_login_and_settings_are_blocked(container):
    async with client_for(container) as client:
        result = await client.post(
            "/ui/login", headers={"Origin": "https://evil.example"}, json={"access_key": "test-admin"}
        )
        assert result.status_code == 403
        await sign_in(client)
        result = await client.put(
            "/ui/settings", headers={"Origin": "https://evil.example"}, json=update().model_dump(mode="json")
        )
        assert result.status_code == 403


async def test_user_cannot_change_settings_or_view_internal_programs(container):
    async with client_for(container) as client:
        await sign_in(client, "test-user")
        assert (await client.get("/ui/settings")).status_code == 403
        programs = (await client.get("/ui/programs")).json()["items"]
        assert programs and all(p["visibility"] == "public" for p in programs)


async def test_build_metrics_are_aggregated_and_admin_only(container):
    for duration, hit in [(100, False), (20, True)]:
        await container.db.event(
            "pipeline_stage",
            {
                "stage": "validation",
                "duration_ms": duration,
                "success": True,
                "cache_hit": hit,
            },
        )
    async with client_for(container) as client:
        await sign_in(client, "test-user")
        assert (await client.get("/ui/build-metrics")).status_code == 403
        await sign_in(client)
        rows = (await client.get("/ui/build-metrics")).json()
        row = next(r for r in rows if r["stage"] == "validation")
        assert row["runs"] == 2 and row["successes"] == 2 and row["cache_hits"] == 1
        assert float(row["avg_duration_ms"]) == 60


async def test_single_use_launch_and_logout_revocation(container):
    token = await container.web_sessions.create_launch()
    async with client_for(container) as client:
        response = await client.post("/ui/login", json={"launch_token": token})
        assert response.status_code == 200
        client.headers["X-Foundry-CSRF"] = response.json()["csrf_token"]
        cookie = client.cookies.get("foundry_session")
        assert (await client.post("/ui/login", json={"launch_token": token})).status_code == 401
        assert (await client.post("/ui/logout")).status_code == 200
        client.cookies.set("foundry_session", cookie)
        assert (await client.get("/ui/settings")).status_code == 401


async def test_settings_are_encrypted_and_shared_with_new_llm_calls(container):
    container.settings.llm_private_hosts = ["api.test"]
    secret = "provider-private-key-not-for-output"
    await container.profiles.save(update(api_key=secret))
    rows = await container.db.fetch("SELECT encrypted_payload FROM agent.console_settings")
    assert secret not in rows[0]["encrypted_payload"]
    async with client_for(container) as client:
        await sign_in(client)
        public = await client.get("/ui/settings")
        assert secret not in public.text and "**********" not in public.text
        assert public.json()["has_api_key"]
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ready"}}]}
        )

    second_store = ProfileStore(container.db, container.settings)
    llm = LLM(
        container.settings,
        container.db,
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        profiles=second_store,
    )
    try:
        assert await llm.call("builder", "Build", "A synthetic request") == "ready"
        assert json.loads(seen[0].content)["model"] == "new-builder"
        assert seen[0].headers["authorization"] == "Bearer " + secret
    finally:
        await llm.close()
    events = await container.db.fetch("SELECT data FROM agent.events")
    assert secret not in str(events)


async def test_changing_provider_does_not_forward_previous_key(container):
    container.settings.llm_private_hosts = ["api.test", "other.test"]
    await container.profiles.save(update(api_key="private-key"))
    change = update()
    change.base_url = "https://other.test/v1"
    with pytest.raises(PolicyError, match="새 API 키"):
        await container.profiles.save(change)
    change.clear_api_key = True
    assert not (await container.profiles.save(change))["has_api_key"]


async def test_connection_test_returns_real_provider_models_without_saving(container):
    container.settings.llm_private_hosts = ["api.test"]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"data": [{"id": "example-model-b"}, {"id": "example-model-a"}]}
            )
        )
    ) as client:
        result = await container.profiles.models(update(api_key="ephemeral-key"), client=client)
    assert result["models"] == ["example-model-a", "example-model-b"]
    assert not await container.db.fetch("SELECT name FROM agent.console_settings")


async def test_model_fetch_errors_do_not_return_credentials(container):
    container.settings.llm_private_hosts = ["api.test"]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "secret-provider-key"})
        )
    ) as client:
        with pytest.raises(PolicyError, match="API 키") as error:
            await container.profiles.models(update(api_key="secret-provider-key"), client=client)
    assert "secret-provider-key" not in str(error.value)


async def test_settings_reject_metadata_addresses(container):
    with pytest.raises(PolicyError):
        await container.profiles.check_destination(
            update().model_copy(update={"base_url": "https://169.254.169.254/v1"})
        )


async def test_programs_filters_and_no_endpoint_exposure(container):
    async with client_for(container) as client:
        await sign_in(client)
        all_programs = (await client.get("/ui/programs")).json()
        assert all_programs["total"] == 8
        internal = (await client.get("/ui/programs?status=internal")).json()
        assert all(p["visibility"] == "internal" for p in internal["items"])
        assert all("endpoint" not in p and "secret_name" not in p for p in all_programs["items"])
        active = (await client.get("/ui/programs?status=ACTIVE&q=calculator")).json()
        assert active["total"] == 1


@pytest.mark.parametrize("base_url", ["not-a-url", "https://api.test:wrong/v1", "https://u:p@api.test/v1"])
async def test_invalid_provider_address_returns_validation_error(container, base_url):
    async with client_for(container) as client:
        await sign_in(client)
        data = update().model_dump(mode="json")
        data["base_url"] = base_url
        assert (await client.put("/ui/settings", json=data)).status_code == 422
        assert not await container.db.fetch("SELECT name FROM agent.console_settings")


async def test_readiness_reflects_saved_model_and_unicode_login_is_rejected(container):
    container.settings.main_model = ""
    container.settings.llm_private_hosts = ["api.test"]
    async with client_for(container) as client:
        assert not (await client.get("/health/ready")).json()["llm_configured"]
        await container.profiles.save(update())
        assert (await client.get("/health/ready")).json()["llm_configured"]
        assert (await client.post("/ui/login", json={"access_key": "잘못된 접속 키"})).status_code == 401
