from .models import Manifest


def schema(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


def seed_manifests():
    definitions = [
        {
            "name": "calculator",
            "description": "정확한 사칙연산 계산기. Decimal arithmetic calculator: add subtract multiply divide.",
            "input_schema": schema({"expression": {"type": "string", "maxLength": 300}}),
            "output_schema": schema({"result": {"type": "string"}}),
            "tags": ["계산", "계산해줘", "calculator", "arithmetic"],
            "examples": [
                {"prompt": "0.1 + 0.2", "input": {"expression": "0.1 + 0.2"}, "output": {"result": "0.3"}}
            ],
            "selection_rules": [
                {
                    "pattern": r"^(?:(?:계산|계산해줘|calculate)\s*:?\s*)?(?P<expression>[\d\s.+*/%()\-]+?)(?:\s*(?:계산해줘|계산해 줘|는 얼마야|=?\?))?$",
                    "fields": {"expression": "string"},
                }
            ],
        },
        {
            "name": "file-reader",
            "description": "관리자 전용: 허용된 파일 디렉터리에서 UTF-8 파일 읽기",
            "visibility": "admin",
            "input_schema": schema({"path": {"type": "string"}}),
            "output_schema": schema({"content": {"type": "string"}}),
        },
        {
            "name": "file-writer",
            "description": "관리자 전용: 허용된 디렉터리에 새 UTF-8 파일 생성. 기존 파일 덮어쓰기 금지",
            "visibility": "admin",
            "side_effects": True,
            "input_schema": schema({"path": {"type": "string"}, "content": {"type": "string"}}),
            "output_schema": schema({"path": {"type": "string"}, "bytes": {"type": "integer"}}),
        },
        {
            "name": "database-query",
            "description": "관리자가 승인한 query_id의 SELECT만 읽기 전용 계정으로 실행",
            "visibility": "admin",
            "input_schema": schema(
                {"query_id": {"type": "string"}, "parameters": {"type": "object"}}, ["query_id"]
            ),
            "output_schema": schema({"rows": {"type": "array", "items": {"type": "object"}}}),
        },
        {
            "name": "http-api-caller",
            "description": "승인된 고정 HTTPS endpoint의 API 어댑터 등록을 위한 템플릿",
            "visibility": "internal",
            "input_schema": schema({}),
            "output_schema": schema({}),
        },
        {
            "name": "web-search-adapter",
            "description": "검색 API 제공자와 secret reference 설정 후 사용하는 어댑터 템플릿",
            "visibility": "internal",
            "input_schema": schema({"query": {"type": "string"}}),
            "output_schema": schema({"results": {"type": "array"}}),
        },
        {
            "name": "python-executor",
            "description": "내부 기능: Registry에 등록된 Python commit을 격리 실행. 임의 코드 직접 실행 금지",
            "visibility": "internal",
            "input_schema": schema({"program_id": {"type": "string"}, "input": {"type": "object"}}),
            "output_schema": schema({"result": {"type": "object"}}),
        },
        {
            "name": "builder-internal",
            "description": "내부 Worker: 생성 테스트 배포 등록 기능. 사용자 Router에서 선택 불가",
            "visibility": "internal",
            "input_schema": schema({"capability": {"type": "string"}}),
            "output_schema": schema({"job_id": {"type": "string"}}),
        },
    ]
    return [Manifest(version="1.0.0", runtime="builtin", execution_type="builtin", **d) for d in definitions]


async def seed(registry):
    for manifest in seed_manifests():
        if not await registry.db.fetch("SELECT id FROM agent.programs WHERE id=%s", (manifest.program_id,)):
            await registry.register(
                manifest, status="ACTIVE" if manifest.name == "calculator" else "DISABLED"
            )
