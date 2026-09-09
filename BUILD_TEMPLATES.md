# 공통 생성 템플릿

Builder는 `build_spec.template`의 `comparison`, `aggregation`, `storage` 선택에 따라
`template_contract(name)`의 함수 계약만 모델에 전달한다. 모델은 `app/main.py`의 업무별
입출력 매핑, 테스트, README를 작성한다. `apply_template(bundle, name)`이 검증된 공통
코드를 추가한 뒤 기존 격리 테스트와 배포를 수행한다. 이 문서는 연결 함수의 계약이며,
템플릿을 적용했다는 사실 자체가 테스트 성공을 뜻하지 않는다.

| 템플릿 | 제공 함수 | 기능 | DB |
|---|---|---|---|
| comparison | `compare(records, filters, sort_by, descending, limit)` | 정확 일치·숫자 범위 조건, 숫자 정렬, 개수 제한 | 비교 자료와 결과 저장용 `store` 함께 제공 |
| aggregation | `aggregate(records, field, group_by)` | 그룹별 개수·합계·평균·최소·최대 | 기본적으로 불필요 |
| storage | `store(input_data)` | JSON 기록 저장·개별 조회·목록 조회 | 중앙 DB의 프로그램 전용 스키마 |

`app/foundry_template.py`는 Worker가 주입하는 예약 파일이다. comparison에는
`app/foundry_storage.py`도 추가한다. 해당 파일의 다른 내용으로 덮어쓰기를 거부하며
소스에 버전 `1.0.0`이 포함된다. 생성 명세 계약에는 원본 SHA-256도 포함한다.
Git에 원본을 포함하므로 다른 서버는 설치된 플랫폼 패키지에 의존하지 않고 실행한다.
고객 입력·원문·비밀키·운영 데이터는 템플릿 원본에 포함하지 않는다.

집계는 Decimal을 사용하고 결과 숫자를 JSON 문자열로 반환한다. 합계는 입력 크기와
지수 제한 안에서 정확하게 계산하며 반복 소수 평균은 유효숫자 50자리로 반올림한다.
비교는 누락값을 범위 조건에 일치시키지 않고, 정렬 시 누락값을 항상 마지막에 둔다.
단위 변환·환율·날짜 해석은 업무 코드에서 명시적으로 처리해야 한다.

저장 함수는 `put` / `get` / `list`를 지원한다. `put`은 `key`와 JSON 객체 `value`,
`get`은 `key`, `list`는 최대 100건의 `limit`과 마지막 키 `after`를 받는다.
비교 업무 코드는 입력 사양·출처·확인일·결과를 value에 저장하고 현재 실행의
`storage` 결과를 최상위로 반환한다. DB 기록은 `template_records(record_key text,
payload jsonb)`와 일반 인덱스를 선언해 Worker가 생성한다. 동일 키 쓰기를 트랜잭션
잠금으로 직렬화하고 생성·수정·재사용·조회를 구분한다. 쓰기 알림은 커밋 후 반환한다.
DB 접속은 Worker가 제공하는 `FOUNDRY_TOOL_DATABASE_URL`만 사용한다.

테스트에서는 고객 자료 대신 합성 자료를 사용한다. 일반/경계/오류 입력 외에 저장 후
조회, 같은 요청 재실행, 변경 입력 갱신을 검증한다. 기존 프로그램 수정 시에도
템플릿 버전·소스·manifest가 바뀌면 다시 검증해야 한다.
