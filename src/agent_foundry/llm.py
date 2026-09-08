import json
import time

import httpx

from .security import PolicyError


class LLM:
    def __init__(self, settings, db, client=None):
        self.settings, self.db = settings, db
        self.client = client or httpx.AsyncClient(timeout=settings.llm_timeout, trust_env=False)

    async def close(self):
        await self.client.aclose()

    async def call(self, role, system, user, *, structured=False, request_id=None):
        model = getattr(self.settings, f"{role}_model")
        if not model:
            raise PolicyError(f"Configure FOUNDRY_{role.upper()}_MODEL")
        started = time.monotonic()
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": getattr(self.settings, f"{role}_max_tokens"),
        }
        if structured:
            payload["response_format"] = {"type": "json_object"}
        success, usage = False, {}
        try:
            headers = {}
            if self.settings.llm_api_key.get_secret_value():
                headers["Authorization"] = f"Bearer {self.settings.llm_api_key.get_secret_value()}"
            response = await self.client.post(
                self.settings.llm_base_url.rstrip("/") + "/chat/completions", json=payload, headers=headers
            )
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage", {})
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
                raise PolicyError("LLM response was incomplete or refused")
            content = choice["message"]["content"]
            result = json.loads(content) if structured else content
            success = True
            return result
        finally:
            await self.db.event(
                "llm",
                {
                    "role": role,
                    "model": model,
                    "success": success,
                    "duration_ms": (time.monotonic() - started) * 1000,
                    "usage": usage,
                },
                request_id,
            )
