import json
import time

import httpx

from .profiles import ProfileStore
from .providers import auth_headers, completion_request, response_text, response_usage
from .security import PolicyError


class LLM:
    def __init__(self, settings, db, client=None, profiles=None):
        self.settings, self.db = settings, db
        self.profiles = profiles
        self.client = client or httpx.AsyncClient(timeout=settings.llm_timeout, trust_env=False)

    async def close(self):
        await self.client.aclose()

    async def call(self, role, system, user, *, structured=False, request_id=None):
        profile = (
            await self.profiles.current()
            if self.profiles
            else ProfileStore(self.db, self.settings).defaults()
        )
        model = getattr(profile, f"{role}_model")
        if not model:
            raise PolicyError(f"Configure FOUNDRY_{role.upper()}_MODEL")
        started = time.monotonic()
        success, usage = False, {}
        try:
            path, payload = completion_request(
                profile, model, system, user, getattr(self.settings, f"{role}_max_tokens"), structured
            )
            response = await self.client.post(
                profile.base_url + path, json=payload, headers=auth_headers(profile), follow_redirects=False
            )
            response.raise_for_status()
            data = response.json()
            usage = response_usage(profile.provider, data)
            content = response_text(profile.provider, data)
            result = json.loads(content) if structured else content
            if structured and not isinstance(result, dict):
                raise PolicyError("LLM must return a JSON object")
            success = True
            return result
        finally:
            await self.db.event(
                "llm",
                {
                    "role": role,
                    "provider": profile.provider,
                    "model": model,
                    "success": success,
                    "duration_ms": (time.monotonic() - started) * 1000,
                    "usage": usage,
                },
                request_id,
            )
