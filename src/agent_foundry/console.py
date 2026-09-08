import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import Field, SecretStr

from .models import StrictModel
from .profiles import ProfileUpdate
from .security import PolicyError


class Login(StrictModel):
    access_key: SecretStr = SecretStr("")
    launch_token: str = Field("", max_length=100)


def console_router(services):
    router = APIRouter()
    sessions, profiles, db = services.web_sessions, services.profiles, services.db

    async def signed_in(request: Request):
        return await sessions.authorize(request)

    async def administrator(request: Request):
        return await sessions.authorize(request, admin=True)

    @router.get("/", include_in_schema=False)
    async def page():
        return FileResponse(Path(__file__).with_name("web") / "index.html")

    @router.get("/ui/session")
    async def session(request: Request):
        current = await sessions.current(request)
        return {"authenticated": bool(current), **(current or {})}

    @router.post("/ui/login")
    async def login(data: Login, request: Request, response: Response):
        sessions.same_origin(request)
        role = None
        if data.launch_token:
            if await sessions.consume_launch(data.launch_token):
                role = "admin"
        else:
            key = data.access_key.get_secret_value()
            if len(key) > 1000:
                raise HTTPException(401, "워크스페이스 접속 키를 확인해주세요.")
            if services.settings.admin_key.get_secret_value() and secrets.compare_digest(
                key.encode(), services.settings.admin_key.get_secret_value().encode()
            ):
                role = "admin"
            elif services.settings.api_key.get_secret_value() and secrets.compare_digest(
                key.encode(), services.settings.api_key.get_secret_value().encode()
            ):
                role = "user"
        if not role:
            raise HTTPException(
                401, "접속 키 또는 연결 링크를 확인해주세요. 일회용 링크는 5분 동안 유효합니다."
            )
        await sessions.logout(request, response)
        return await sessions.create(role, request, response)

    @router.post("/ui/logout", dependencies=[Depends(signed_in)])
    async def logout(request: Request, response: Response):
        await sessions.logout(request, response)
        return {"authenticated": False}

    @router.get("/ui/overview", dependencies=[Depends(signed_in)])
    async def overview(request: Request):
        session = await sessions.current(request)
        profile = await profiles.public()
        counts = await db.fetch(
            """SELECT count(*) AS total,
            count(*) FILTER (WHERE status='ACTIVE') AS active,
            count(*) FILTER (WHERE status='DISABLED') AS disabled
            FROM agent.programs WHERE %s OR manifest->>'visibility'='public'""",
            (session["role"] == "admin",),
        )
        return {
            "programs": counts[0],
            "connection": {
                k: profile[k] for k in ("provider", "has_api_key", "models_configured", "main_model")
            },
            "role": session["role"],
        }

    @router.get("/ui/programs", dependencies=[Depends(signed_in)])
    async def programs(request: Request, q: str = "", status: str = "all", offset: int = 0):
        if (
            len(q) > 200
            or status not in {"all", "ACTIVE", "DISABLED", "internal"}
            or not 0 <= offset <= 100000
        ):
            raise HTTPException(422, "프로그램 필터를 확인해주세요.")
        session = await sessions.current(request)
        admin = session["role"] == "admin"
        condition = """WHERE (%s OR manifest->>'visibility'='public')
            AND (name ILIKE %s OR description ILIKE %s)
            AND (%s='all' OR status=%s OR (%s='internal' AND manifest->>'visibility'='internal'))"""
        params = (admin, "%" + q + "%", "%" + q + "%", status, status, status)
        rows = await db.fetch(
            """SELECT id,name,description,version,runtime,execution_type,status,tags,
            usage_count,success_count,failure_count,avg_latency_ms,last_used_at,git_commit,
            manifest->>'visibility' AS visibility,examples,input_schema,output_schema
            FROM agent.programs """
            + condition
            + " ORDER BY (status='ACTIVE') DESC,name LIMIT 50 OFFSET %s",
            (*params, offset),
        )
        total = await db.fetch("SELECT count(*) AS total FROM agent.programs " + condition, params)
        return {"items": rows, "total": total[0]["total"], "offset": offset, "page_size": 50}

    @router.get("/ui/settings", dependencies=[Depends(administrator)])
    async def settings():
        return await profiles.public()

    @router.put("/ui/settings", dependencies=[Depends(administrator)])
    async def save(data: ProfileUpdate):
        try:
            return await profiles.save(data)
        except PolicyError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.post("/ui/connection/test", dependencies=[Depends(administrator)])
    async def test_connection(data: ProfileUpdate):
        try:
            return await profiles.models(data)
        except PolicyError as exc:
            raise HTTPException(422, str(exc)) from None

    return router
