import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .config import Settings
from .console import console_router
from .container import Container
from .models import AgentRequest, StrictModel
from .security import PolicyError


class ExecuteRequest(StrictModel):
    input: dict


class RollbackRequest(StrictModel):
    program_id: UUID
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")


def create_app(settings=None, container=None):
    settings = settings or Settings()
    services = container or Container(settings)

    @asynccontextmanager
    async def lifespan(app):
        await services.open()
        yield
        await services.close()

    app = FastAPI(title="Agent Foundry", version="0.1.0", lifespan=lifespan)
    app.state.services = services

    def auth(expected, authorization):
        if not expected:
            raise HTTPException(503, "API authentication is not configured")
        supplied = (authorization or "").removeprefix("Bearer ")
        if not secrets.compare_digest(expected.encode(), supplied.encode()):
            raise HTTPException(401, "Invalid credentials")

    async def user_auth(request: Request, authorization: str | None = Header(None)):
        if authorization is not None:
            auth(settings.api_key.get_secret_value(), authorization)
        else:
            await services.web_sessions.authorize(request)

    async def admin_auth(request: Request, authorization: str | None = Header(None)):
        if authorization is not None:
            auth(settings.admin_key.get_secret_value(), authorization)
        else:
            await services.web_sessions.authorize(request, admin=True)

    @app.middleware("http")
    async def browser_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path not in {"/docs", "/redoc", "/docs/oauth2-redirect"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        if request.url.path.startswith(("/ui/", "/admin/", "/v1/")) or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(PolicyError)
    async def policy_error(request, exc):
        # Diagnostic details remain in administrator logs, never the public API.
        return JSONResponse(status_code=422, content={"error": "policy_or_configuration_error"})

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        try:
            await services.db.fetch("SELECT id FROM agent.programs LIMIT 1")
            profile = await services.profiles.current()
            return {"status": "ready", "llm_configured": bool(profile.main_model)}
        except Exception:
            raise HTTPException(503, "Registry unavailable") from None

    @app.post("/v1/agent", dependencies=[Depends(user_auth)])
    async def agent(request: AgentRequest):
        try:
            return await services.service.respond(request)
        except PolicyError:
            raise
        except Exception:
            raise HTTPException(
                502, "Request processing failed; inspect administrator execution logs"
            ) from None

    @app.get("/v1/programs/search", dependencies=[Depends(user_auth)])
    async def search(q: str, source: str = "installed"):
        if not q.strip() or len(q) > settings.max_prompt_chars or source not in {"installed", "github"}:
            raise HTTPException(422, "Invalid query length")
        searcher = services.catalog.search if source == "github" else services.search
        return [c.model_dump(mode="json") for c in await searcher.search(q)]

    @app.get("/admin/jobs/{job_id}", dependencies=[Depends(admin_auth)])
    async def job(job_id: UUID):
        result = await services.queue.get(job_id)
        if not result:
            raise HTTPException(404, "Job not found")
        return result

    @app.get("/admin/programs", dependencies=[Depends(admin_auth)])
    async def programs():
        return await services.registry.health_report()

    @app.get("/admin/metrics", dependencies=[Depends(admin_auth)])
    async def metrics():
        return {
            "programs": await services.registry.health_report(),
            "llm": await services.db.fetch("""SELECT data->>'role' AS role,count(*) AS calls,
                    sum(COALESCE((data->'usage'->>'total_tokens')::bigint,0)) AS recorded_tokens,
                    avg((data->>'duration_ms')::float) AS avg_latency_ms
                    FROM agent.events WHERE event_type='llm' GROUP BY data->>'role'"""),
            "routes": await services.db.fetch("""SELECT data->>'route' AS route,count(*) AS requests
                    FROM agent.events WHERE event_type='request_completed' GROUP BY data->>'route'"""),
        }

    @app.post("/admin/programs/{program_id}/execute", dependencies=[Depends(admin_auth)])
    async def execute(program_id: UUID, request: ExecuteRequest):
        return await services.executor.execute(program_id, request.input, uuid4(), admin=True)

    @app.post("/admin/reconcile", dependencies=[Depends(admin_auth)])
    async def reconcile():
        return {"job_id": await services.queue.enqueue("RECONCILE", "reconcile", {})}

    @app.post("/admin/rollback", dependencies=[Depends(admin_auth)])
    async def rollback(request: RollbackRequest):
        return {
            "job_id": await services.queue.enqueue(
                "ROLLBACK", f"{request.program_id}:{request.commit}", request.model_dump(mode="json")
            )
        }

    app.include_router(console_router(services))
    app.mount("/assets", StaticFiles(directory=Path(__file__).with_name("web")), name="web-assets")
    return app


app = create_app()
