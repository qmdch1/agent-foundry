import hashlib
import secrets

from fastapi import HTTPException

COOKIE_NAME = "foundry_session"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class WebSessions:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    @staticmethod
    def same_origin(request):
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if request.headers.get("origin") != expected:
            raise HTTPException(403, "같은 웹 주소에서 다시 시도해주세요.")

    async def current(self, request):
        token = request.cookies.get(COOKIE_NAME, "")
        if not token or len(token) > 100:
            return None
        rows = await self.db.fetch(
            """SELECT role,csrf_token FROM agent.web_sessions
            WHERE token_hash=%s AND expires_at>now()""",
            (digest(token),),
        )
        return rows[0] if len(rows) == 1 else None

    async def authorize(self, request, *, admin=False):
        session = await self.current(request)
        if not session:
            raise HTTPException(401, "워크스페이스에 연결해주세요.")
        if admin and session["role"] != "admin":
            raise HTTPException(403, "설정 변경은 관리자만 할 수 있습니다.")
        if request.method not in {"GET", "HEAD"}:
            self.same_origin(request)
            if not secrets.compare_digest(
                request.headers.get("x-foundry-csrf", "").encode(), session["csrf_token"].encode()
            ):
                raise HTTPException(403, "화면을 새로고침한 뒤 다시 시도해주세요.")
        return session

    async def create(self, role, request, response):
        await self.db.execute("DELETE FROM agent.web_sessions WHERE expires_at<=now()")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        await self.db.execute(
            """INSERT INTO agent.web_sessions(token_hash,role,csrf_token,expires_at)
            VALUES (%s,%s,%s,now()+%s*interval '1 hour')""",
            (digest(token), role, csrf, self.settings.web_session_hours),
        )
        response.set_cookie(
            COOKIE_NAME,
            token,
            httponly=True,
            samesite="strict",
            path="/",
            secure=request.url.scheme == "https",
            max_age=self.settings.web_session_hours * 3600,
        )
        return {"authenticated": True, "role": role, "csrf_token": csrf}

    async def logout(self, request, response):
        await self.db.execute(
            "DELETE FROM agent.web_sessions WHERE token_hash=%s",
            (digest(request.cookies.get(COOKIE_NAME, "")),),
        )
        response.delete_cookie(COOKIE_NAME, path="/")

    async def create_launch(self):
        token = secrets.token_urlsafe(32)
        await self.db.execute("DELETE FROM agent.web_launches WHERE expires_at<=now()")
        await self.db.execute(
            "INSERT INTO agent.web_launches(token_hash,expires_at) VALUES (%s,now()+interval '5 minutes')",
            (digest(token),),
        )
        return token

    async def consume_launch(self, token):
        if not token or len(token) > 100:
            return False
        async with self.db.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "DELETE FROM agent.web_launches WHERE token_hash=%s AND expires_at>now() RETURNING token_hash",
                    (digest(token),),
                )
            ).fetchone()
        return bool(row)
