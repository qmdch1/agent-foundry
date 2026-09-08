import httpx
import pytest

from agent_foundry.api import create_app

pytestmark = pytest.mark.integration


def local_client(container, host="localhost:8000"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container.settings, container)),
        base_url="http://" + host,
    )


async def test_local_admin_opens_web_settings_and_admin_api_without_credentials(container):
    container.settings.local_admin = True
    container.settings.llm_private_hosts = ["api.test"]
    async with local_client(container) as client:
        session = await client.get("/ui/session")
        assert session.json() == {"authenticated": True, "role": "admin", "auth_mode": "local_admin"}
        assert "set-cookie" not in session.headers
        programs = (await client.get("/ui/programs")).json()["items"]
        assert any(p["visibility"] == "internal" for p in programs)
        assert (await client.get("/ui/settings")).status_code == 200
        # A stale user or invalid key must not downgrade the configured local administrator.
        assert (
            await client.get("/admin/programs", headers={"Authorization": "Bearer old-key"})
        ).status_code == 200
        response = await client.post("/v1/agent", json={"prompt": "0.1 + 0.2"})
        assert response.json()["result"] == {"result": "0.3"}
        calculator = next(p for p in programs if p["name"] == "calculator")
        example = calculator["examples"][0]
        response = await client.post(
            f"/admin/programs/{calculator['id']}/execute", json={"input": example["input"]}
        )
        assert response.status_code == 200 and response.json() == example["output"]
        response = await client.put(
            "/ui/settings",
            json={
                "provider": "compatible",
                "base_url": "https://api.test/v1",
                "main_model": "test-main",
                "router_model": "test-router",
                "evaluator_model": "test-evaluator",
                "builder_model": "test-builder",
            },
        )
        assert response.status_code == 200
        assert (await container.profiles.public())["provider"] == "compatible"
        assert (await client.post("/ui/logout")).json()["authenticated"]
    assert not await container.db.fetch("SELECT * FROM agent.web_sessions")


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://another.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
async def test_local_admin_does_not_accept_other_websites(container, headers):
    container.settings.local_admin = True
    async with local_client(container) as client:
        assert (await client.get("/ui/session", headers=headers)).status_code == 403
        response = await client.post("/v1/agent", headers=headers, json={"prompt": "1+1"})
        assert response.status_code == 403
        assert (await client.get("/admin/programs", headers=headers)).status_code == 403


async def test_local_mode_can_restore_auth_and_does_not_apply_to_other_hosts(container):
    assert not container.settings.local_admin
    container.settings.local_admin = True
    async with local_client(container, "agent.example") as client:
        assert (await client.get("/ui/session")).json() == {"authenticated": False}
        assert (await client.get("/admin/programs")).status_code == 401
    async with local_client(container) as client:
        assert (await client.get("/ui/settings")).status_code == 200
        container.settings.local_admin = False
        assert (await client.get("/ui/settings")).status_code == 401
        assert (await client.get("/admin/programs")).status_code == 401
        response = await client.get("/admin/programs", headers={"Authorization": "Bearer test-admin"})
        assert response.status_code == 200
