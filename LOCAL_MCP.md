# 개인 PC에서 로컬 MCP로 사용하기

[메인 저장소](https://github.com/qmdch1/agent-foundry) · [공유 프로그램](https://github.com/qmdch1/agent-tools)

Agent Foundry는 각 사용자의 PC에서 실행합니다. 운영자가 공용 서버를 제공할 필요가 없습니다.
AI 앱은 STDIO로 로컬 MCP 프로세스를 시작하며, Foundry의 로컬 DB와 Executor를 사용합니다.
로컬 웹/API와 PostgreSQL, Builder는 Docker Compose로 실행합니다. MCP는 별도의 HTTP 포트를 열지 않습니다.

```text
개인의 AI 앱 / AI API 클라이언트
            ↓ STDIO MCP
개인 PC의 Agent Foundry
  ├─ 검색 → 프로그램 ID → Executor → 결과
  ├─ PostgreSQL: 개인 Registry + 프로그램별 데이터 스키마
  └─ Queue → 별도 Builder → 생성·테스트 → 로컬 Git commit → 사용
                       └─ 공유를 선택하면 개인 GitHub 저장소에 push
```

GitHub 저장소에는 소스만 공유합니다. 다른 사람의 DB·프롬프트·통계·API 키를 받거나 보내지 않습니다.
사용자가 보내는 질문·도구 결과는 사용하는 AI 제공자가 처리하며, Builder를 켜면 필요한 검토 자료와
생성 명세가 별도로 설정한 AI 제공자에게 전달됩니다. 로컬 실행이 모든 AI 처리를 오프라인으로 만든다는 뜻은 아닙니다.

## 1. 준비 및 설치

Windows는 Docker Desktop의 Linux 컨테이너, macOS는 Docker Desktop, Linux는 Docker Engine/Compose를 사용합니다.
Git, Python 3.12 이상, uv가 필요합니다. Python은 `uv python install 3.12`로 설치할 수도 있습니다.
Docker 엔진을 먼저 실행하세요. Windows에서 WSL을 사용한다면 해당 배포판의 Docker 연동이 필요합니다.

처음 사용할 작업 폴더에서 다음을 실행합니다. Linux/macOS 터미널과 Windows PowerShell에서 같은 명령을 사용합니다.

```bash
git clone https://github.com/qmdch1/agent-foundry.git
cd agent-foundry
uv sync --frozen
uv run foundry init-env --local
docker compose config --quiet
docker compose --profile images build sandbox-image
docker compose up -d --build api builder
```

Linux는 시작 전에 `stat -c '%g' /var/run/docker.sock`의 값을 `.env`의 `DOCKER_GID`에 설정합니다.
새 설치의 `--local`은 로컬 관리자 모드와 로컬 릴리스를 켜고 Git push는 끕니다.
기존 `.env`를 덮어쓰지 않으므로 기존 설치에서는 필요한 값을 편집기로 변경하세요.
두 저장소가 공개되기 전에는 GitHub 읽기 인증이 필요합니다.

```dotenv
FOUNDRY_LOCAL_ADMIN=true
FOUNDRY_LOCAL_RELEASES_ENABLED=true
FOUNDRY_GIT_PUSH=false
```

공유 프로그램은 Worker가 `agent-tools`를 별도 Docker 볼륨에 자동 clone합니다. 사용만 할 때는
호스트에서 두 번째 저장소를 직접 clone할 필요가 없습니다. 소스를 편집하려면 메인 폴더 옆에
별도로 clone하세요. 두 저장소를 서로의 내부에 넣지 않습니다.

웹: [http://localhost:8000](http://localhost:8000/). 로컬 연결만 허용하는 Compose 포트 바인딩을 유지합니다.
같은 PC에서 같은 Compose 프로젝트를 두 번 설치하면 데이터 볼륨을 공유하므로 이 절차는 PC당 하나의
개인 설치를 전제로 합니다. 서로 다른 PC는 독립된 DB와 볼륨을 사용합니다.

## 2. Codex에 MCP 등록

AI 앱에 전달할 것은 **실행 명령과 절대 경로**입니다. 공개 MCP 목록 등록, 도메인, OAuth 서비스는 필요 없습니다.
아래 `C:/work/agent-foundry`를 실제 clone한 폴더로 바꾸세요. Linux/macOS는 `/absolute/path/agent-foundry` 형태입니다.

Codex 설정 파일의 예시:

설치 폴더에서 `uv run foundry mcp-config`를 실행하면 현재 절대 경로가 반영된 Codex 설정을 출력합니다.
다른 앱용 JSON은 `uv run foundry mcp-config --format json`을 사용합니다. 설정 파일을 자동으로 덮어쓰지는 않습니다.

```toml
[mcp_servers.agent_foundry]
command = "docker"
args = ["compose", "--project-directory", "C:/work/agent-foundry", "-f", "C:/work/agent-foundry/docker-compose.yml", "exec", "-T", "api", "foundry-mcp"]
startup_timeout_sec = 30
tool_timeout_sec = 60
```

Codex의 신뢰된 프로젝트에서는 `.codex/config.toml`에 범위를 제한할 수 있습니다. 사용자 전체 설정은
`~/.codex/config.toml`을 사용합니다. 기존 파일이 있다면 위 항목만 추가하고 다른 설정을 덮어쓰지 마세요.
Windows 사용자 전체 설정은 일반적으로 `%USERPROFILE%\.codex\config.toml`에 있습니다.

또는 **설정 → MCP 서버 → 서버 추가 → STDIO**에서 동일한 명령과 인수를 지정합니다.
저장한 뒤 MCP 연결을 다시 시작하고, 새 대화의 도구 목록에서 Foundry를 확인합니다.
`docker`를 찾지 못하면 실행 파일의 전체 경로를 사용합니다. 설정 파일에 API 키나 DB 비밀번호를 넣지 않습니다.

이 명령은 이미 실행 중인 로컬 `api` 컨테이너 안에 전용 STDIO 프로세스를 시작합니다.
`-T`를 빼면 터미널 제어 문자가 MCP 통신을 방해할 수 있습니다. MCP 프로세스는 stdout을
프로토콜 전용으로 사용하고 로그는 stderr로 보냅니다. 연결이 끝나도 Builder와 DB는 계속 실행됩니다.

## 3. 다른 MCP 지원 AI 앱

`mcpServers` JSON 설정을 사용하는 앱은 다음 구조를 사용합니다. 설정 파일의 위치와 재시작 절차는 앱마다 다릅니다.

```json
{
  "mcpServers": {
    "agent-foundry": {
      "command": "docker",
      "args": ["compose", "--project-directory", "C:/work/agent-foundry", "-f", "C:/work/agent-foundry/docker-compose.yml", "exec", "-T", "api", "foundry-mcp"]
    }
  }
}
```

STDIO를 지원하는 **로컬 클라이언트**가 필요합니다. 원격 HTTP MCP만 지원하는 웹 AI에는 이 명령을 직접 등록할 수 없습니다.
공개 클라우드 서버가 사용자의 `localhost`나 Docker 프로세스에 접근할 수 있다고 가정하지 않습니다.

## 4. AI 연결과 비용

- MCP 검색·기존 프로그램 실행은 Foundry의 Main/Router 모델을 다시 호출하지 않습니다. 연결한 AI 앱이 결과를 해석합니다.
- 자동 생성에는 웹 **연결 및 모델 설정**에서 개인 API 키와 네 역할의 모델을 설정합니다.
  백그라운드 중복 판별은 Router를 사용할 수 있으며 웹 프롬프트는 Main도 사용합니다. 한 활성 제공자를 네 역할이 공유합니다.
- 기존 AI 앱의 로그인·구독이 Builder API 키로 자동 전달되지 않습니다. Builder 비용은 각자의 설정한 API 계정에 발생합니다.
- 생성 없이 사용하려면 `FOUNDRY_BUILDER_ENABLED=false`를 설정하고 `docker compose up -d api builder`로 반영합니다.
- MCP 자체를 개인 설정에 등록하는 별도 등록비는 없습니다. PC 자원·AI 사용료·선택한 Git 서비스 비용은 별개입니다.

Foundry의 사용 통계는 Foundry 내부 호출만 측정합니다. 외부 AI 앱의 토큰을 자동 수집하지 않으며,
화면의 절약 토큰은 비교 기준을 이용한 계산값입니다. 생성 토큰은 제공자가 보고한 Builder 사용량을
누적하고, 과거에 코드 크기로 환산한 값은 별도 메타데이터로 구분합니다.

## 5. 제공 도구 및 사용 규칙

| MCP 도구 | 동작 |
| --- | --- |
| `search_programs` | 자연어로 설치된 프로그램 또는 Git 카탈로그 Top-K 조회 |
| `execute_program` | 공개·사용 가능 프로그램을 ID와 검증된 입력으로 실행; 데이터 쓰기가 있을 수 있음 |
| `install_program` | 카탈로그의 프로그램을 별도 Worker 설치 작업으로 등록 |
| `submit_build_review` | 답변·참고 자료·선택 명세를 암호화해 평가 Queue에 저장 |
| `job_status` | 작업 하나의 상태와 생성된 프로그램/후속 작업 ID 조회 |
| `sync_catalog` | 설정한 Git 저장소의 목록 갱신을 Worker에 요청 |

프로그램 수가 늘어나도 이 여섯 도구만 노출합니다. 프로그램별 스키마는 검색 후보에만 포함합니다.
검색은 마지막으로 동기화한 Git 목록을 사용합니다. 관리자 SQL·파일·shell·Git credential 기능은 MCP에 노출하지 않습니다.
최대 응답 길이는 `FOUNDRY_MCP_OUTPUT_MAX_CHARS`로 제한하며 잘린 응답은 `truncated=true`로 표시합니다.

사용을 허용한 프로젝트의 AI 규칙 파일에 아래 지침을 추가할 수 있습니다.

```text
이 프로젝트에서 재사용 가능한 계산·데이터 처리가 필요하면 Foundry의 설치 프로그램을 먼저 검색한다.
검색된 ID와 입력 스키마로 실행하며, 결과와 데이터 저장 안내를 사용자에게 전달한다.
준비된 프로그램이 없으면 사용자 질문에 먼저 답한다. 설치·생성 완료를 기다리지 않는다.
답변을 전달한 후 재사용 가치가 있는 경우에만 최소한의 일반화한 내용을 생성 검토에 넘긴다.
비밀값·불필요한 개인정보·과거 대화는 넘기지 않는다. 다른 프로젝트나 대화에는 적용하지 않는다.
queued를 완료라고 표현하지 않는다. 작업 상태를 반복 조회하며 사용자 답변을 지연시키지 않는다.
```

MCP 서버도 위 흐름을 instructions로 제공하지만, 규칙은 매번 실행을 보장하는 스케줄러가 아닙니다.
특히 최종 답변 후 도구 호출을 지원하지 않는 AI 앱에서는 호스트의 후속 실행 기능이 필요합니다.
MCP가 모든 대화를 감시하거나 화면에 답변이 전달됐는지 확인하지 않습니다.

### 간단한 구현을 Spark에 위임

이 저장소의 AI 작업 규칙은 계획·작업 분해·최종 검토를 사용자가 선택한 모델에 맡깁니다.
입출력과 테스트 기준이 명확한 작은 함수·파서·형식 변환 등은 별도 `gpt-5.3-codex-spark`
서브에이전트에 위임합니다. 설계·권한·DB 구성·배포 판단은 상위 모델이 담당합니다.

호스트의 모델별 서브에이전트 또는 별도 Codex CLI 프로세스가 필요합니다. Spark를 사용할 수 없으면
그 사실을 알리고 상위 모델로 진행합니다. 이는 AI 작업 규칙이며, MCP 등록만으로 Foundry API
Builder의 모델이 바뀌지는 않습니다. 구체적인 실행 범위는 [작업 규칙](AGENTS.md#host-ai-delegation-policy)을 참고하세요.

## 6. AI API를 직접 사용하는 앱

Worker는 평가와 생성·설치를 별도 슬롯에서 처리합니다. 평가 중 기존 공유 프로그램을 찾으면
`INSTALL_QUEUED`와 `install_job_id`를 반환하고 설치 슬롯으로 넘깁니다. 이 상태는 설치 완료가 아닙니다.
테스트 실패 후에는 이전 코드를 참고해 변경된 파일만 생성할 수 있으며, 검증·배포·push 절차는 그대로 적용합니다.

AI API 키만으로 MCP가 연결되지는 않습니다. 앱이 MCP 도구 스키마를 모델에 전달하고 모델이 요청한
도구 호출을 MCP 클라이언트로 실행해야 합니다. 실행 가능한 OpenAI 호환 예제는
[`examples/api_agent.py`](examples/api_agent.py)에 있습니다.

개인의 비밀 관리 방식으로 `AI_BASE_URL`, `AI_API_KEY`, `AI_MODEL`을 환경변수에 설정한 뒤 실행합니다.
이 값들은 예제의 외부 AI용이며 Foundry 웹의 Builder 설정과 별개입니다. 주소는 Chat Completions의 base URL입니다.

```bash
uv run python examples/api_agent.py "0.1 + 0.2를 계산해줘"
# 답변 출력 이후 생성 검토도 허용할 때만:
uv run python examples/api_agent.py "행 목록의 부서별 합계 계산 방법을 알려줘" --review
```

예제는 검색·실행·설치 세 도구만 모델에 제공하고 도구 왕복 횟수를 제한합니다. `--review`는 답변을
출력한 뒤 별도로 검토를 요청하며 생성 완료를 기다리지 않습니다. 실제 API 모델의 function calling
지원이 필요합니다. 예제 자체가 웹 검색을 제공하지 않으므로 최신 상품 정보 등을 조회했다고 주장하지 않습니다.

## 7. 로컬 보관과 GitHub 공유

기본 로컬 설치는 생성·테스트 성공 후 **로컬 Git commit**을 만들고 그 고정 commit을 검증해 활성화합니다.
`published=false`이며 공용 카탈로그에는 올라가지 않습니다. 공개 저장소의 쓰기 권한도 필요 없습니다.
로컬 commit은 Docker의 `tools-checkout` 볼륨에 있으므로 삭제하지 마세요.

자동 공유를 원한다면 **처음 설치하기 전에** `agent-tools`를 개인 계정으로 Fork하고 다음처럼 초기화합니다.

```bash
uv run foundry init-env --local --repository https://github.com/YOUR_ACCOUNT/agent-tools.git
```

이후 `.env`의 `FOUNDRY_GIT_PUSH=true`를 지정하고, `.worker.env`에 개인 저장소의 Contents 읽기·쓰기
권한이 있는 `FOUNDRY_GIT_TOKEN`을 설정합니다. 키를 코드·채팅·명령 인수에 넣지 않습니다.
`docker compose up -d api builder`로 반영하면 다음 생성·확장부터 개인 저장소에 push한 뒤 활성화합니다.
이미 만든 로컬 commit을 push 모드 전환만으로 자동 업로드하지는 않습니다.
다음 push에는 같은 브랜치의 이전 로컬 commit도 포함될 수 있으므로, 공유를 켜기 전에 해당 Git 이력을 확인합니다.

다른 사람은 해당 Fork 주소를 자신의 새 설치에 지정해 공개된 프로그램을 검색·설치할 수 있습니다.
원본 공유 저장소에 기여하려면 Fork에서 Pull Request를 보내세요. 모든 사용자에게 원본 쓰기 권한을 주거나
관리자의 GitHub 토큰을 배포하지 않습니다. 원본 변경은 각 Fork에서 Sync fork로 가져옵니다.

현재 카탈로그는 설치당 **하나의 승인된 원격 저장소**를 사용합니다. 원본과 여러 Fork를 동시에 합치는 기능은 없습니다.
한번 설치한 환경에서 원격 주소만 바꾸면 기존 commit과 캐시가 맞지 않아 거부될 수 있습니다.
개인 Fork는 초기 설정 때 선택하고, 기존 로컬 commit을 이동할 때는 볼륨·DB를 먼저 보존한 뒤 명시적으로 이관합니다.

## 8. 업데이트·복구·문제 해결

```bash
git pull --ff-only
uv sync --frozen
docker compose up -d --build api builder
docker compose exec -T builder foundry reconcile
```

MCP 연결도 다시 시작해 새 프로세스를 사용합니다. 일반 종료는 `docker compose stop`, 재시작은
`docker compose up -d api builder`입니다. `docker compose down -v`는 개인 데이터 볼륨을 삭제하므로
일반 업데이트나 종료 명령으로 사용하지 않습니다.

복구에는 로컬 PostgreSQL 백업, `.env`의 암호화 키, `foundry-state`, 그리고 미공유 commit이 있는
`tools-checkout` 볼륨이 필요합니다. GitHub에 올린 소스만으로 개인 데이터는 복구되지 않습니다.
`reconcile`은 ACTIVE 프로그램을 복구하며 빈 DB에 공유 프로그램을 전부 설치하는 명령은 아닙니다.

| 증상 | 확인할 내용 |
| --- | --- |
| MCP 시작 실패 | Docker 실행, `api` 컨테이너 상태, 절대 경로, `-T`, 실행 파일 PATH |
| 검색 결과 없음 | `installed`/`github` 구분, Worker 및 카탈로그 동기화 상태 |
| 검토가 PENDING | 별도 `builder` 컨테이너가 실행 중인지 확인 |
| 평가·생성 실패 | 개인 API 키, 역할별 모델, JSON 지원, 사용 한도 |
| Git push 실패 | 개인 Fork 주소, Worker 토큰 권한, 브랜치 정책 |
| 프로그램 실행 실패 | 로컬 웹의 상태 확인, 관리자 로그 확인, 필요 시 reconcile |

## 라이선스 및 참고

두 저장소는 MIT 라이선스입니다. 의존 패키지·AI 서비스·외부 자료에는 각자의 라이선스와 약관이 적용됩니다.
AI가 생성한 결과물과 외부 자료의 재배포 권리를 MIT 파일만으로 보장하지 않습니다.

- [Codex MCP 설정](https://developers.openai.com/codex/mcp/)
- [MCP 서버와 STDIO 연결](https://modelcontextprotocol.io/docs/develop/build-server)
- [공식 Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [관리·복구 상세 문서](DEPLOYMENT.md)
