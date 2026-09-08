"""Check a running local API without printing credentials or private inputs."""

import json
import time

import httpx

from agent_foundry.config import Settings

settings = Settings()
cases = [
    ("0.1 + 0.2", {"result": "0.3"}),
    (
        "CSV value 열 값 1, 2, 3의 통계를 계산해줘",
        {"count": 3, "missing_count": 0, "sum": "6", "mean": "2", "min": "1", "max": "3"},
    ),
]
with httpx.Client(base_url="http://localhost:8000", timeout=60) as client:
    report = []
    for prompt, expected in cases:
        start = time.monotonic()
        response = client.post(
            "/v1/agent",
            headers={"Authorization": "Bearer " + settings.api_key.get_secret_value()},
            json={"prompt": prompt},
        )
        response.raise_for_status()
        result = response.json()
        assert result["result"] == expected and result["route"] == "deterministic", result
        report.append(
            {
                "programs": result["programs"],
                "route": result["route"],
                "result": result["result"],
                "duration_ms": round((time.monotonic() - start) * 1000, 2),
            }
        )
    print(json.dumps({"passed": True, "checks": report}, ensure_ascii=False, indent=2))
