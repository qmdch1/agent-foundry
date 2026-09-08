"""Provider presets and wire formats; models and credentials remain user configuration."""

import re
from dataclasses import asdict, dataclass
from typing import Literal
from urllib.parse import urlsplit

from .security import PolicyError

ProviderId = Literal[
    "openai", "anthropic", "gemini", "deepseek", "groq", "mistral", "openrouter", "compatible"
]


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    description: str
    base_url: str
    key_url: str
    docs_url: str
    protocol: str = "chat"
    token_field: str = "max_tokens"
    console_url: str = ""
    setup_url: str = ""
    setup_hint: str = ""


PROVIDERS = {
    p.id: p
    for p in [
        Provider(
            "openai",
            "OpenAI",
            "OpenAI 모델",
            "https://api.openai.com/v1",
            "https://platform.openai.com/api-keys",
            "https://platform.openai.com/docs/api-reference/chat",
            token_field="max_completion_tokens",
            console_url="https://platform.openai.com/",
            setup_url="https://developers.openai.com/api/docs/quickstart",
            setup_hint="OpenAI Platform에 로그인하고 API Keys에서 사용할 프로젝트의 키를 만드세요.",
        ),
        Provider(
            "anthropic",
            "Anthropic Claude",
            "Claude 모델",
            "https://api.anthropic.com/v1",
            "https://platform.claude.com/settings/keys",
            "https://platform.claude.com/docs/en/api/messages/create",
            "anthropic",
            console_url="https://platform.claude.com/",
            setup_url="https://platform.claude.com/docs/en/get-started",
            setup_hint="Claude Console에 로그인하고 Settings → API keys에서 키를 만드세요.",
        ),
        Provider(
            "gemini",
            "Google Gemini",
            "Google AI Studio API",
            "https://generativelanguage.googleapis.com/v1beta",
            "https://aistudio.google.com/apikey",
            "https://ai.google.dev/api/generate-content",
            "gemini",
            console_url="https://aistudio.google.com/",
            setup_url="https://ai.google.dev/gemini-api/docs/api-key?hl=ko",
            setup_hint="Google 계정으로 AI Studio에 로그인하고 프로젝트를 선택해 새 API 키를 만드세요.",
        ),
        Provider(
            "deepseek",
            "DeepSeek",
            "DeepSeek 모델",
            "https://api.deepseek.com",
            "https://platform.deepseek.com/api_keys",
            "https://api-docs.deepseek.com/",
            console_url="https://platform.deepseek.com/",
            setup_url="https://api-docs.deepseek.com/",
            setup_hint="DeepSeek Platform에 로그인하고 API keys에서 키를 만드세요.",
        ),
        Provider(
            "groq",
            "Groq",
            "Groq에서 제공하는 모델",
            "https://api.groq.com/openai/v1",
            "https://console.groq.com/keys",
            "https://console.groq.com/docs/openai",
            token_field="max_completion_tokens",
            console_url="https://console.groq.com/",
            setup_url="https://console.groq.com/docs/quickstart",
            setup_hint="Groq Console에 로그인하고 사용할 프로젝트의 API Keys에서 키를 만드세요.",
        ),
        Provider(
            "mistral",
            "Mistral AI",
            "Mistral 모델",
            "https://api.mistral.ai/v1",
            "https://console.mistral.ai/",
            "https://docs.mistral.ai/api/endpoint/chat",
            console_url="https://console.mistral.ai/",
            setup_url="https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key",
            setup_hint="Mistral Studio에 로그인하고 왼쪽 API Keys 메뉴에서 Create new key를 선택하세요.",
        ),
        Provider(
            "openrouter",
            "OpenRouter",
            "여러 회사의 모델을 한 API로",
            "https://openrouter.ai/api/v1",
            "https://openrouter.ai/settings/keys",
            "https://openrouter.ai/docs/api/reference/overview",
            console_url="https://openrouter.ai/settings/keys",
            setup_url="https://openrouter.ai/docs/cookbook/get-started/quickstart",
            setup_hint="OpenRouter에 로그인하고 Keys에서 사용할 워크스페이스의 API 키를 만드세요.",
        ),
        Provider(
            "compatible",
            "직접 연결",
            "OpenAI 호환 API · 사설 모델",
            "",
            "",
            "",
            setup_hint="서비스 관리자에게 받은 API 주소와 키를 입력하세요. 키가 필요 없는 서버는 키를 비워두세요.",
        ),
    ]
}


def provider_options():
    return [asdict(p) for p in PROVIDERS.values()]


def infer_provider(base_url):
    url = base_url.rstrip("/")
    for p in PROVIDERS.values():
        if url == p.base_url:
            return p.id
    # Preserve the wire format of existing custom/compatible connections.
    return "openai" if urlsplit(url).hostname == "api.openai.com" else "compatible"


def auth_headers(profile):
    protocol = PROVIDERS[profile.provider].protocol
    key = profile.api_key.get_secret_value()
    if protocol == "anthropic":
        return {"anthropic-version": "2023-06-01", **({"x-api-key": key} if key else {})}
    if protocol == "gemini":
        return {"x-goog-api-key": key} if key else {}
    return {"Authorization": "Bearer " + key} if key else {}


def completion_request(profile, model, system, user, token_limit, structured):
    spec = PROVIDERS[profile.provider]
    if structured:
        system += "\nReturn only one valid JSON object. Do not include Markdown fences or surrounding text."
    if spec.protocol == "anthropic":
        return "/messages", {
            "model": model,
            "max_tokens": token_limit,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    if spec.protocol == "gemini":
        model = model.removeprefix("models/")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,149}", model):
            raise PolicyError("Gemini 모델 이름을 확인해주세요.")
        generation = {"maxOutputTokens": token_limit}
        if structured:
            generation["responseMimeType"] = "application/json"
        return f"/models/{model}:generateContent", {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation,
        }
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        spec.token_field: token_limit,
    }
    if structured:
        payload["response_format"] = {"type": "json_object"}
    return "/chat/completions", payload


def response_usage(provider, data):
    protocol = PROVIDERS[provider].protocol
    if protocol == "gemini":
        raw = data.get("usageMetadata", {})
        if not raw:
            return {}
        prompt = raw.get("promptTokenCount", 0)
        completion = raw.get("candidatesTokenCount", 0) + raw.get("thoughtsTokenCount", 0)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": raw.get("totalTokenCount", prompt + completion),
        }
    raw = data.get("usage", {})
    if protocol == "anthropic" and raw:
        prompt = sum(
            raw.get(k, 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        completion = raw.get("output_tokens", 0)
        return {
            **raw,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }
    return raw


def response_text(provider, data):
    protocol = PROVIDERS[provider].protocol
    if protocol == "anthropic":
        if data.get("stop_reason") != "end_turn" or (data.get("stop_details") or {}).get("type") == "refusal":
            raise PolicyError("LLM response was incomplete or refused")
        text = "".join(b["text"] for b in data["content"] if b.get("type") == "text")
    elif protocol == "gemini":
        candidates = data.get("candidates", [])
        if (
            not candidates
            or candidates[0].get("finishReason") != "STOP"
            or data.get("promptFeedback", {}).get("blockReason")
        ):
            raise PolicyError("LLM response was incomplete or refused")
        text = "".join(
            b["text"] for b in candidates[0]["content"]["parts"] if "text" in b and not b.get("thought")
        )
    else:
        choice = data["choices"][0]
        if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
            raise PolicyError("LLM response was incomplete or refused")
        text = choice["message"]["content"]
    if not isinstance(text, str) or not text.strip():
        raise PolicyError("LLM returned no answer text")
    return text
