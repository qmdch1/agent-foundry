import asyncio
import ipaddress
import json
import socket
from urllib.parse import urlsplit

import httpx
from pydantic import Field, SecretStr, field_validator

from .models import StrictModel
from .providers import PROVIDERS, ProviderId, auth_headers, infer_provider
from .security import PolicyError, decrypt_payload, encrypt_payload


class ConnectionProfile(StrictModel):
    provider: ProviderId = "openai"
    base_url: str = Field("https://api.openai.com/v1", max_length=2000)
    api_key: SecretStr = SecretStr("")
    main_model: str = Field("", max_length=150)
    router_model: str = Field("", max_length=150)
    evaluator_model: str = Field("", max_length=150)
    builder_model: str = Field("", max_length=150)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        try:
            endpoint = urlsplit(value)
            endpoint.port
        except ValueError:
            raise ValueError("API 주소의 호스트와 포트를 확인해주세요.") from None
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("API 주소는 인증정보·쿼리문자열 없는 HTTP(S) 주소여야 합니다.")
        return value.rstrip("/")

    @field_validator("main_model", "router_model", "evaluator_model", "builder_model")
    @classmethod
    def valid_model(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError("모델 이름에 제어문자를 사용할 수 없습니다.")
        return value.strip()


class ProfileUpdate(ConnectionProfile):
    provider: ProviderId
    base_url: str = Field(max_length=2000)
    api_key: SecretStr | None = None
    clear_api_key: bool = False


class ProfileStore:
    """Server-side encrypted settings shared by all API and Worker processes."""

    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def defaults(self):
        return ConnectionProfile(
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
            provider=self.settings.llm_provider or infer_provider(self.settings.llm_base_url),
            **{
                role + "_model": getattr(self.settings, role + "_model")
                for role in ("main", "router", "evaluator", "builder")
            },
        )

    async def current(self):
        rows = await self.db.fetch("SELECT encrypted_payload FROM agent.console_settings WHERE name='llm'")
        if not rows:
            return self.defaults()
        if len(rows) != 1:
            raise PolicyError("연결 설정을 확인할 수 없습니다. 관리자에게 문의해주세요.")
        return ConnectionProfile.model_validate(
            decrypt_payload(rows[0]["encrypted_payload"], self.settings.job_encryption_key.get_secret_value())
        )

    async def public(self):
        profile = await self.current()
        return {
            **profile.model_dump(exclude={"api_key"}),
            "provider_name": PROVIDERS[profile.provider].name,
            "has_api_key": bool(profile.api_key.get_secret_value()),
            "models_configured": all(
                getattr(profile, r + "_model") for r in ("main", "router", "evaluator", "builder")
            ),
        }

    async def merge(self, update: ProfileUpdate):
        previous = await self.current()
        key = previous.api_key
        if update.clear_api_key:
            key = SecretStr("")
        elif update.api_key and update.api_key.get_secret_value():
            key = update.api_key
        elif (
            update.provider != previous.provider or update.base_url != previous.base_url
        ) and key.get_secret_value():
            raise PolicyError("제공자 주소를 바꿀 때는 새 API 키를 입력하거나 기존 키 삭제를 선택해주세요.")
        return ConnectionProfile(**update.model_dump(exclude={"api_key", "clear_api_key"}), api_key=key)

    async def check_destination(self, profile):
        endpoint = urlsplit(profile.base_url)
        # Private/local compatible providers must be explicitly configured by the server owner.
        trusted = set(self.settings.llm_private_hosts)
        if endpoint.hostname in trusted:
            return
        if endpoint.scheme != "https":
            raise PolicyError("HTTPS 주소를 사용해주세요. 사설 제공자는 서버의 허용 목록 설정이 필요합니다.")
        try:
            addresses = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(
                    endpoint.hostname, endpoint.port or 443, type=socket.SOCK_STREAM
                ),
                timeout=5,
            )
        except (OSError, TimeoutError):
            raise PolicyError("API 서버 주소를 찾을 수 없습니다.") from None
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise PolicyError("사설·로컬 주소는 서버의 허용 목록에 등록해야 합니다.")

    async def save(self, update):
        profile = await self.merge(update)
        await self.check_destination(profile)
        data = profile.model_dump(exclude={"api_key"})
        data["api_key"] = profile.api_key.get_secret_value()
        encrypted = encrypt_payload(data, self.settings.job_encryption_key.get_secret_value())
        async with self.db.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('console-settings',0))")
            await conn.execute("DELETE FROM agent.console_settings WHERE name='llm'")
            await conn.execute(
                "INSERT INTO agent.console_settings(name,encrypted_payload) VALUES ('llm',%s)", (encrypted,)
            )
        await self.db.event(
            "connection_settings_saved",
            {
                "provider": profile.provider,
                "models": {
                    r: getattr(profile, r + "_model") for r in ("main", "router", "evaluator", "builder")
                },
                "key_present": bool(profile.api_key.get_secret_value()),
            },
        )
        return await self.public()

    async def models(self, update=None, client=None):
        profile = await self.merge(update) if update else await self.current()
        if profile.provider != "compatible" and not profile.api_key.get_secret_value():
            raise PolicyError("선택한 제공자의 API 키를 입력해주세요.")
        await self.check_destination(profile)
        headers = auth_headers(profile)
        protocol = PROVIDERS[profile.provider].protocol
        own_client = client is None
        client = client or httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False)
        models, cursor, total_bytes = set(), None, 0
        try:
            for _ in range(10):
                params = {}
                if protocol == "anthropic":
                    params = {"limit": 1000, **({"after_id": cursor} if cursor else {})}
                elif protocol == "gemini":
                    params = {"pageSize": 1000, **({"pageToken": cursor} if cursor else {})}
                async with client.stream(
                    "GET",
                    profile.base_url + "/models",
                    headers=headers,
                    params=params,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in (401, 403):
                        raise PolicyError("API 키 또는 모델 조회 권한을 확인해주세요.")
                    if response.status_code == 429:
                        raise PolicyError("제공자의 요청 한도에 도달했습니다. 잠시 후 다시 시도해주세요.")
                    if response.status_code != 200:
                        raise PolicyError(
                            "모델 목록을 가져오지 못했습니다. API 주소와 /models 지원 여부를 확인해주세요."
                        )
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        total_bytes += len(chunk)
                        if total_bytes > 2_000_000:
                            raise PolicyError("제공자의 모델 목록이 허용된 크기를 초과했습니다.")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError("Expected a model-list object")
                entries = result.get("models", []) if protocol == "gemini" else result["data"]
                if not isinstance(entries, list):
                    raise ValueError("Expected a list of models")
                for item in entries:
                    if not isinstance(item, dict):
                        continue
                    if protocol == "gemini" and "generateContent" not in item.get(
                        "supportedGenerationMethods", []
                    ):
                        continue
                    capabilities = item.get("capabilities") or {}
                    if (
                        protocol == "chat"
                        and isinstance(capabilities, dict)
                        and capabilities.get("completion_chat") is False
                    ):
                        continue
                    model = item.get("name") if protocol == "gemini" else item.get("id")
                    if isinstance(model, str) and len(model) <= 150:
                        models.add(model.removeprefix("models/") if protocol == "gemini" else model)
                next_cursor = (
                    result.get("nextPageToken")
                    if protocol == "gemini"
                    else result.get("last_id")
                    if result.get("has_more")
                    else None
                )
                if not next_cursor or next_cursor == cursor or len(models) >= 1000:
                    break
                cursor = next_cursor
            return {"connected": True, "models": sorted(models)[:1000], "provider": profile.provider}
        except PolicyError:
            raise
        except httpx.HTTPError:
            raise PolicyError("API 서버에 연결할 수 없습니다. 주소와 네트워크 상태를 확인해주세요.") from None
        except (ValueError, KeyError, TypeError):
            raise PolicyError("제공자의 모델 목록 형식을 확인할 수 없습니다.") from None
        finally:
            if own_client:
                await client.aclose()
