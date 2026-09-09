# 관리·배포와 AI 연결

[메인 프로그램](https://github.com/qmdch1/agent-foundry) · [서브 프로그램](https://github.com/qmdch1/agent-tools)

개인 PC의 기본 설치·MCP 연결은 [LOCAL_MCP.md](LOCAL_MCP.md)를 먼저 보세요. 이 문서는 관리·복구 및 선택적인 서버 운영을 설명합니다.

이 문서는 저장소의 Compose, CLI, API 구현을 기준으로 설명합니다. 프로그램 생성·확장 기능은
테스트로 검증하며, 실제 유료 LLM의 생성 품질·속도는 사용할 API 키와 모델로 별도 확인해야 합니다.
응답 시간을 보장하는 수치나 모든 Codex 대화를 자동 수집하는 기능은 포함하지 않습니다.

## 1. 연결 구조

```text
웹 또는 외부 AI 클라이언트
  ├─ 사용자 요청 → Agent API → 기존 프로그램 검색·실행 / Main LLM 답변
  └─ 답변 후 생성 검토 요청 → PostgreSQL 작업 Queue
                                  ↓
                         별도 Builder Worker
                         평가·기존 도구 확인
                         템플릿 생성 / 기존 도구 확장
                         격리 테스트 → Git push
                         commit 재조회 → 배포·등록

중앙 PostgreSQL: agent 스키마 + DB 사용 프로그램별 tool_<UUID> 스키마
GitHub agent-tools: 프로그램 소스·테스트·manifest·DB 정의
```

API와 Builder는 별도 서비스입니다. API는 생성 완료를 기다리지 않습니다. 설치·DB 준비·Git
작업은 Worker가 담당합니다. 생성 도구는 제한된 Docker 컨테이너에서 실행되며 관리 API 키나
Docker socket을 받지 않습니다.

## 2. 새 서버 설치

Git, Python 3.12 이상, uv, Docker Engine와 Docker Compose가 필요합니다. 다음은 Linux 기준입니다.
두 GitHub 저장소가 비공개이면 먼저 호스트 Git의 credential helper에 접근 계정을 연결합니다.

```bash
mkdir -p ~/projects
cd ~/projects
git clone https://github.com/qmdch1/agent-foundry.git
git clone https://github.com/qmdch1/agent-tools.git
cd agent-foundry
uv sync --frozen
uv run foundry init-env
```

`init-env`는 무작위 비밀값이 든 `.env`를 만들며 기존 파일은 덮어쓰지 않습니다. `.env`를
편집기로 열어 설정하고 파일 내용이나 비밀값을 채팅·로그·Git에 복사하지 않습니다.

Linux에서는 Docker socket 그룹 ID를 확인해 `.env`의 `DOCKER_GID`에 넣습니다.

```bash
stat -c '%g' /var/run/docker.sock
```

이 PC에서만 로그인 없이 사용하려면 `FOUNDRY_LOCAL_ADMIN=true`를 지정하고 Compose의
`127.0.0.1` 포트 바인딩을 유지합니다. 다른 서버에 공개할 때는 `false`와 인증을 사용하고,
TLS 및 접근 제어를 제공하는 리버스 프록시를 연결합니다.

```bash
docker compose config --quiet
docker compose --profile images build sandbox-image
docker compose up -d --build api builder
docker compose ps
```

PostgreSQL과 migration 서비스가 함께 시작됩니다. 웹은 `http://localhost:8000/`, 준비 상태는
`http://localhost:8000/health/ready`에서 확인합니다. 인증 모드는 `.env`의 관리자 키로 웹에
로그인합니다. 초기 계산기는 LLM 없이 사용할 수 있습니다.

Compose에서는 `agent-tools`를 `tools-checkout` 볼륨의 `/tools-repository`에 별도로 clone합니다.
호스트의 `../agent-tools`와 같은 폴더가 아닙니다. 로컬 Python 방식으로 Worker를 실행할 때만
`FOUNDRY_TOOL_REPOSITORY_ROOT=../agent-tools`가 호스트 체크아웃을 가리킵니다.

## 3. AI 제공자와 모델 연결

웹의 **연결 및 모델 설정**에서 다음 순서로 설정합니다.

1. 제공자 또는 OpenAI 호환 직접 연결을 선택합니다.
2. 해당 제공자의 공식 콘솔에서 API 키를 발급합니다.
3. API 주소와 키를 입력하고 모델 목록을 조회합니다.
4. Main, Router, Evaluator, Builder 모델을 각각 지정하고 저장합니다.

한 활성 제공자 연결을 네 역할이 함께 사용합니다. 저장한 웹 설정은 환경변수보다 우선하며,
암호화된 중앙 설정을 API와 Worker가 다음 호출부터 같이 읽습니다. 로그인 링크는 공식 콘솔
바로가기이며 OAuth 연결이나 ChatGPT 구독 인증을 뜻하지 않습니다.

모델 목록 조회 성공만으로 JSON 응답·코드 생성 권한까지 확인된 것은 아닙니다. 실제 사용할
모델로 일반 질문 1건과 합성 데이터 기반 생성 1건을 실행해 확인합니다. API 사용료가 발생할 수
있으며, 이 저장소의 합성 LLM 테스트는 실제 제공자의 결과를 대신하지 않습니다.

환경변수 연결을 선택한다면 `.env`의 `FOUNDRY_LLM_PROVIDER`, `FOUNDRY_LLM_BASE_URL`,
`FOUNDRY_LLM_API_KEY` 및 네 역할의 `FOUNDRY_*_MODEL`을 지정합니다. 사설 제공자는
`FOUNDRY_LLM_PRIVATE_HOSTS`에 호스트를 명시적으로 허용합니다.

## 4. GitHub 읽기·쓰기 연결

호스트에서 메인 저장소를 clone/update하는 인증과 **컨테이너 Worker의 agent-tools 인증은
별개**입니다. 호스트에서 `gh auth login`을 했다는 사실만으로 Worker가 인증되지는 않습니다.

Worker에는 `agent-tools`만 접근하는 토큰을 연결합니다. 비공개 저장소 읽기와 프로그램
commit push가 가능해야 하며, GitHub에서 해당 저장소의 Contents 읽기·쓰기 권한을 부여합니다.
기본 브랜치에 직접 push를 금지하는 정책이면 자동 push가 실패하므로 저장소 정책에 맞는
빌드 브랜치와 `FOUNDRY_GIT_BRANCH`를 선택합니다. 브랜치는 원격에 먼저 존재해야 합니다.

`.worker.env`를 편집기로 만들고 `FOUNDRY_GIT_TOKEN`에 토큰을 넣습니다. 이 파일은 Git에서
제외되며 Compose가 Worker에만 전달합니다. 권한을 제한하고 토큰을 명령 인수나 URL에 넣지
않습니다. 메인 `.env`에는 원격 주소와 브랜치 등 비밀이 아닌 설정을 둡니다.

```dotenv
FOUNDRY_TOOL_REPOSITORY=https://github.com/qmdch1/agent-tools.git
FOUNDRY_GIT_BRANCH=main
FOUNDRY_GIT_PUSH=true
```

```bash
chmod 600 .worker.env
docker compose up -d builder
docker compose exec -T builder foundry catalog-sync
```

`catalog-sync`는 원격 읽기 확인입니다. 쓰기 권한은 실제 생성·확장 작업의 push에서 확인됩니다.
공유 모드의 push 실패는 성공으로 표시하거나 새 버전을 ACTIVE로 바꾸지 않습니다. 로컬 모드(`FOUNDRY_LOCAL_RELEASES_ENABLED=true`, `FOUNDRY_GIT_PUSH=false`)는 push 없이 고정 로컬 commit을 검증해 활성화하며, 공유 카탈로그에는 등록하지 않습니다. 로컬 commit만 남았다면
인증을 수정해 해당 commit을 push한 뒤 다음 명령으로 고정 버전을 배포할 수 있습니다.

```bash
docker compose exec -T builder foundry deploy tools/프로그램이름 전체40자리commit
```

## 5. 답변 내용과 Builder 연결

### 이 시스템의 웹/API로 질문하는 경우

`POST /v1/agent`에 `allow_build=true`로 요청하면 Main 답변과 짧은 생성 명세를 같은 LLM 호출에서
받습니다. 답변·참고 내용·명세를 암호화된 평가 Job으로 전달하고, Worker가 재사용/비용 평가 후
생성 또는 확장을 결정합니다. 생성 명세는 목적·입출력·처리 단계·독립 검증 조건·DB 필요 여부·
템플릿 종류를 포함합니다. Main 답변을 테스트의 정답으로 그대로 채택하지 않습니다.

이 API는 응답 전 짧은 Queue 저장만 수행합니다. Worker가 클라이언트의 화면 표시 완료를
확인하는 프로토콜은 아닙니다. 응답 수신 후 검토를 엄격히 시작하려면 아래 외부 연결처럼
답변 완료 후 별도의 생성 검토 요청을 보냅니다.

### Codex 등 외부 AI가 먼저 답변하는 경우

외부 클라이언트가 **답변 전송을 완료한 다음**, 별도의 후속 작업으로
`POST /v1/build-reviews`를 호출합니다. 이것은 검토 Queue 등록 API이며 프로그램을 즉시
생성하라고 강제하는 API가 아닙니다. `202`와 `evaluation_job_id`를 받고 생성 대기를 종료합니다.
완료 여부는 관리자 `GET /admin/jobs/{id}` 또는 웹 작업 목록에서 확인합니다.

```json
{
  "prompt": "매출 자료를 부서별로 합산해줘",
  "answer": "부서별로 집계한 답변의 필요한 부분",
  "reference_material": "사용한 열의 의미와 확인한 처리 기준",
  "build_spec": {
    "objective": "행 목록의 부서별 금액 집계",
    "inputs": ["rows: 부서와 금액이 있는 행 목록"],
    "outputs": ["부서별 합계"],
    "steps": ["입력 검증", "부서별 집계"],
    "acceptance_checks": ["그룹별 합계의 총합이 전체 금액 합계와 같음"],
    "requires_db": false,
    "template": "aggregation"
  }
}
```

`build_spec`는 선택 항목입니다. `prompt`는 최대 12,000자, `answer`는 50,000자,
`reference_material`은 20,000자까지 받으며 답변/참고 자료는 설정된 컨텍스트 한도로 줄여
평가에 전달합니다. 필요한 정보만 보내고 실제 고객 자료를 소스·예시로 복사하지 않습니다.

인증 모드에서는 외부 서버가 비밀 관리 계층에서 사용자 API 키를 읽어 `Authorization: Bearer`
헤더로 전달합니다. 브라우저 코드나 Git에 키를 넣지 않습니다. 다음은 외부 서버의 호출 예입니다.

```python
import os
import httpx

async def submit_review_after_answer(payload: dict):
    # 사용자 답변을 보낸 뒤 외부 서비스의 별도 작업에서 호출합니다.
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            os.environ["FOUNDRY_SERVER_URL"].rstrip("/") + "/v1/build-reviews",
            headers={"Authorization": "Bearer " + os.environ["FOUNDRY_API_KEY"]},
            json=payload,
        )
        response.raise_for_status()
        return response.json()
```

외부 서비스는 검토 제출 실패를 별도 기록하고, 이미 전달한 사용자 답변을 취소하거나 생성
완료를 기다리지 않습니다. `FOUNDRY_SERVER_URL`은 위 예제의 외부 클라이언트용 변수입니다.

**이 API만 배포하면 모든 Codex 대화가 자동 연결되는 것은 아닙니다.** 외부 AI의 후속 호출
또는 호스트 연결 코드가 있어야 합니다. 자동 검토는 사용자가 명시적으로 허용한 프로젝트·대화에만 적용하고 다른 대화나 과거 대화를 수집하지 않습니다.

## 6. 중앙 DB와 다른 서버 설치

메인 메타데이터는 `agent`, 저장 기능이 있는 프로그램은 `tool_<program UUID hex>` 스키마를
사용합니다. 프로그램마다 PostgreSQL 컨테이너를 만들지 않습니다. Worker가 선언된 테이블과
일반 인덱스를 추가하고 전용 계정으로 실제 연결을 확인합니다. 기존 데이터와 프로그램 사용
통계는 업데이트 시 유지합니다. 순수 계산 프로그램에는 빈 스키마를 만들지 않습니다.

새 플랫폼에 프로그램만 공유하려면 같은 `agent-tools` 원격에 연결하고 다음을 실행한 뒤
웹의 **GitHub 공유 프로그램**에서 설치를 요청합니다.

```bash
docker compose exec -T builder foundry catalog-sync
```

기존 플랫폼을 다른 서버에서 복구하려면 전용 DB 백업과 `FOUNDRY_JOB_ENCRYPTION_KEY`를 함께
복원하고 DB 이름을 유지합니다. 미공유 로컬 commit이 있다면 `tools-checkout` 볼륨도 함께 복원해야 합니다. 역할을 복원하지 않는 방법을 택했다면 관리 계정으로
`--no-owner --no-acl` 방식으로 DB를 복원한 뒤 아래 reconcile이 프로그램 권한을 재구성합니다.
실제 백업 파일 경로·복원 대상은 운영 절차에서 확정합니다.

```bash
docker compose exec -T builder foundry reconcile
```

reconcile은 Registry의 ACTIVE 프로그램마다 고정 Git commit을 조회해 이미지·실행 정보를
재구성합니다. 빈 Registry에 모든 공유 프로그램을 일괄 설치하는 명령이 아닙니다.
Git은 운영 상품 자료·HRMS 데이터·암호화 키를 담지 않으므로 코드 clone만으로 데이터가 복구되지
않습니다. 여러 Executor 서버를 운영하면 각 서버의 Docker 이미지와 실행 receipt가 필요합니다.

## 7. 캐시와 단계별 시간

Compose의 `foundry-state` 볼륨을 API/Worker가 공유합니다. `/state/validation-cache`에는
성공한 격리 검증의 증거가, `/state/deployments`에는 commit별 실행 receipt가 있습니다.
이미지와 의존성 레이어는 Docker 엔진에 보관됩니다. 재시작 시 볼륨을 유지해야 재사용됩니다.

캐시는 코드·테스트·manifest·승인 의존성·기본 이미지·검증 구현·실행 제한 등이 같을 때만
사용합니다. 삭제되거나 만료되면 재검증합니다. 캐시가 있어도 운영 DB migration과 연결 확인은
생략하지 않습니다. 배포/확장에 실패하면 기존 ACTIVE 버전은 유지합니다.

| 환경변수 | 기본값 | 역할 |
| --- | --- | --- |
| `FOUNDRY_MAIN_BUILD_SPEC_ENABLED` | `true` | Main 답변과 생성 명세를 한 호출에서 정리 |
| `FOUNDRY_AUTO_EXTENSION_ENABLED` | `true` | 적합한 기존 프로그램의 호환 확장 허용 |
| `FOUNDRY_BUILD_CONTEXT_MAX_CHARS` | `6000` | 답변/참고 자료 각각의 전달 길이 제한 |
| `FOUNDRY_EXTENSION_SOURCE_MAX_CHARS` | `60000` | 확장할 기존 코드의 전달 한도 |
| `FOUNDRY_VALIDATION_CACHE_ENABLED` | `true` | 성공한 격리 검증 재사용 |
| `FOUNDRY_VALIDATION_CACHE_TTL_SECONDS` | `86400` | 검증 유효 시간, `0`이면 재사용 안 함 |
| `FOUNDRY_VALIDATION_POLICY_VERSION` | `1` | 관리자가 정책 변경 시 올리는 캐시 구분값 |
| `FOUNDRY_BUILDER_ENABLED` | `true` | 생성 검토/Builder 사용 여부 |
| `FOUNDRY_BUILDER_RETRY_COUNT` | `2` | 최초 생성 이후 수정 재시도 상한 |

환경변수를 바꾼 뒤 `docker compose up -d api builder`로 서비스를 재생성합니다. 운영 설정을
변경하지 않아도 검증 구현 파일 변경이나 기본 이미지 변경은 캐시 키를 바꿉니다.

단계별 기록은 중앙 DB의 `agent.events`에서 조회합니다. 다음 SQL은 원문 프롬프트나 암호화된
작업 payload를 출력하지 않습니다.

관리자는 웹 **프로그램 → 별도 에이전트 작업**에서 최근 7일 단계별 평균 시간과 성공·재사용
횟수를 확인할 수 있습니다. 같은 집계는 관리자 세션의 `GET /ui/build-metrics`로 제공합니다.

```sql
SELECT created_at, request_id,
       data->>'stage' AS stage,
       data->>'program_id' AS program_id,
       data->>'duration_ms' AS duration_ms,
       data->>'success' AS success,
       data->>'cache_hit' AS cache_hit
FROM agent.events
WHERE event_type = 'pipeline_stage'
ORDER BY created_at DESC
LIMIT 100;
```

`image_build`, `validation`, `migration_health`, `deployment` 등 실제 기록된 단계별 시간을
비교해 지연 원인을 확인합니다. 상위 단계 시간에는 하위 단계가 포함될 수 있으므로 전부
합산해 전체 시간이라고 계산하지 않습니다. 일부 단계는 program_id/commit, 일부는 request_id로
연결하며 모든 이벤트에 모든 식별자가 있는 것은 아닙니다.

## 8. 메인 버전 업데이트와 롤백

운영 DB와 암호화 키의 복구 가능성을 확인한 후 메인 저장소를 갱신합니다. 기존 `.env`,
`.worker.env`, DB 볼륨과 state 볼륨은 유지합니다.

```bash
git pull --ff-only
uv sync --frozen
docker compose config --quiet
docker compose --profile images build sandbox-image
docker compose up -d --build api builder
docker compose exec -T builder foundry reconcile
```

`sandbox-image`를 변경하면 기존 검증 증거는 재사용하지 않습니다. 프로그램을 이전 안정
버전으로 되돌릴 때는 Registry에 성공 증거가 있는 전체 commit을 지정합니다.

```bash
docker compose exec -T builder foundry rollback 프로그램UUID 이전안정버전40자리commit
```

이 롤백은 코드/실행 버전을 되돌립니다. 기존 DB 데이터를 과거 시점으로 되돌리거나 추가된
테이블·열을 삭제하지 않습니다. 운영 데이터 복원은 별도 백업 절차로 수행합니다.
