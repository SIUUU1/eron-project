# ClinicalNLP integration contract

이 문서는 API1을 변경하지 않고 ClinicalNLP 초안 생성 기능만 ER:ON 본 프로젝트에
이식할 때 사용하는 단일 기준이다. 구현자는 ClinicalNLP 내부 구조가 아니라 이 문서의
interface와 소유권을 기준으로 작업한다.

관련 세부 기준:

- Ollama Cloud adapter, 출력 검증, 개인정보 게이트는
  [`ollama-cloud-integration.md`](ollama-cloud-integration.md)를 따른다.
- OCI VectorDB 아티팩트, Block Volume, 백업·롤백은
  [`oci-vector-runtime.md`](oci-vector-runtime.md)를 따른다.

## 목표 흐름

```text
API1 Whisper JSON
  → ER:ON backend adapter
  → ClinicalNLP module
  → Ollama Cloud gemma4:31b + local dictionary/UMLS/policy retrieval
  → clinical-workflow-v2
  → ER:ON draft UI
```

이번 이식의 완료 지점은 편집 가능한 응급기록 초안 표시다. 기록 저장, 의료진 서명,
최종 완료 처리와 KCD 확정은 후속 범위다.

## Module ownership

| Module | Owns | Does not own |
|---|---|---|
| API1 | 음성 입력, STT, Whisper JSON 생성 | 임상 초안, 의료용어 검색, Ollama 호출 |
| ER:ON backend adapter | 요청 검증, ClinicalNLP 호출, transport 오류 변환, 응답 계약 검증 | 프롬프트, RAG 판정, 임상 초안 생성 |
| ClinicalNLP | 번역, 의료용어 검색, 후보 판정, 초안, Guardrail, 정책 근거, `clinical-workflow-v2` | 환자·방문 DB 저장, 의료진 인증 |
| ER:ON frontend | 생성 요청, 초안 표시, 근거·오류 표시, 사용자 편집 상태 | Ollama API key, VectorDB 접근, 자동 확정 |
| OCI artifact runtime | 사전·VectorDB 버전, checksum, 배포·롤백 | 환자 대화 또는 환자별 임상 데이터 |

ClinicalNLP는 하나의 깊은 module로 유지한다. ER:ON 호출자가 알아야 하는 interface는
초안 생성 요청과 `clinical-workflow-v2` 응답뿐이다. LLM provider, UMLS worker,
의료사전, VectorDB와 정책 검색은 ClinicalNLP 내부 seam이다.

## Internal ClinicalNLP interface

ClinicalNLP 컨테이너는 ER:ON 내부 네트워크에서 다음 endpoint를 제공한다.

```http
POST /v2/clinical-workflows
Content-Type: application/json
```

요청 본문은 API1 Whisper JSON을 그대로 사용한다. 최소 입력 계약은 다음과 같다.

```json
{
  "segments": [
    {
      "id": "seg_0001",
      "start": 0.0,
      "end": 2.0,
      "text": "합성 또는 승인된 비식별 STT 문장"
    }
  ]
}
```

각 segment는 고유한 문자열 `id`, 역전되지 않은 `start`·`end`, 보존 가능한 `text`를
가져야 한다. 상위 API1 메타데이터는 ClinicalNLP가 필요하지 않은 경우에도 제거하지
않고 전달할 수 있다.

응답은 `clinical-workflow-v2`다. 정본 JSON Schema는 이식 단계에서 다음 위치에 둔다.

```text
services/clinicalnlp/contracts/clinical-workflow-v2.schema.json
```

필수 최상위 필드:

```text
schema_version        = clinical-workflow-v2
processing_status     = completed | partial | failed
record_status         = NOT_STARTED | DRAFT | COMPLETED
workflow_phase
validation
completed_at
api3
api2
query_expansion
candidate_decisions
audit
draft
errors
```

초안 생성 응답은 항상 `record_status=DRAFT`,
`workflow_phase=DRAFT_GENERATION`, `completed_at=null`이어야 한다. LLM, 검색기 또는
정책 DB가 완료 상태나 서명을 만들 수 없다.

## ER:ON backend interface

프론트가 호출하는 공개 endpoint는 다음과 같다.

```http
POST /api/clinical-records/transcribe
Content-Type: multipart/form-data

POST /api/clinical-records/draft
Content-Type: application/json
```

음성 파일을 선택하면 `/transcribe`가 API1의 Whisper JSON을 반환하고 프론트는 그
segment를 대화 기록에 먼저 표시한다. 초안 생성 요청 본문은 Internal ClinicalNLP
interface와 동일한 Whisper JSON이다. backend adapter는 payload를 임상적으로
해석하거나 재작성하지 않는다. 입력 계약을 확인한 뒤 `RECORD_AI_URL`의
`/v2/clinical-workflows`로 전달하고, 응답을 정본 Schema로 검증해 반환한다.

기존 `POST /api/clinical-records/draft/audio` 통합 경로는 호환성을 위해 유지하지만,
운영 UI는 STT 결과를 먼저 확인할 수 있도록 `/transcribe`와 `/draft`를 순서대로 호출한다.

HTTP 상태 계약:

| Status | Meaning |
|---|---|
| `200` | 유효한 `clinical-workflow-v2`; 임상 단계 실패는 본문의 `processing_status`와 `errors`로 표현 |
| `400` | Whisper JSON 입력 계약 위반 |
| `502` | ClinicalNLP 응답이 JSON 또는 `clinical-workflow-v2` 계약을 위반 |
| `503` | `RECORD_AI_URL` 미설정 또는 ClinicalNLP 연결 불가 |
| `504` | backend가 정한 ClinicalNLP 요청 timeout 초과 |

초기 timeout은 180초로 둔다. 합성 27-segment 냉시작 기준으로 관측된 전체 처리시간은
98.5초였으며, 이 값은 SLA가 아니라 timeout 설정의 근거다. nginx도 이 endpoint에만
180초 이상의 read timeout을 적용한다.

생성 endpoint는 DB에 기록을 저장하지 않는다. timeout 또는 사용자의 재시도는 동일
방문 기록을 중복 저장하거나 자동 반영할 수 없다.

## Draft invariants

- 모든 임상값은 STT segment 근거로 역추적할 수 있어야 한다.
- RAW STT와 번역·수정·후보 표현은 서로 다른 필드로 보존한다.
- 검색 점수는 진단 확률이나 규칙 위반 확률이 아니다.
- UMLS·VectorDB 후보는 의료진 검토 전 자동 확정하지 않는다.
- 초안의 `validation.status`는 `PASS`, `REVIEW_REQUIRED`, `BLOCK` 중 하나다.
- `BLOCK`은 초안 반환을 막지 않으며 후속 완료 처리만 제한한다.
- 정책 DB 장애는 결정론적 Guardrail 판정을 변경하지 않는다.
- `partial` 응답도 유효한 초안·번역·후보를 보존한다.
- frontend는 `partial`·`failed`·fallback 상태를 성공 초안처럼 숨기지 않는다.

## Medical-term retrieval order

운영 초안 생성은 다음 순서를 고정한다.

```text
RAW 보존
→ 전체 문장 영문 번역
→ UMLS span 추출
→ UMLS surface·canonical 사전/Vector 검색
→ 미해결 번역 구간 n-gram fallback
→ 공식 한국어 정본 RAW exact fallback
→ 후보 병합
```

번역 전에 전체 사전이나 VectorDB를 조회하지 않는다. RAW exact는 응급의학,
검사·처치, 해부학, 의약품 성분의 공식 한국어 정본만 서비스 시작 시 만든 인메모리
인덱스로 검색한다. 제품명, KCD, 별칭, 퍼지·Vector 검색은 RAW exact 경로에 포함하지
않는다. UMLS가 이미 해결한 동일 사전 entity는 RAW exact 후보로 중복 추가하지 않으며,
UMLS 장애 시에는 RAW exact가 비차단 안전망으로 동작한다.

`umls-primary-policy-v2`부터 UMLS semantic type에 맞는 collection만 Vector
검색한다. 같은 검색어의 로컬 exact 후보가 확인되면 Vector 검색을 생략하고, 의미
유형이 없거나 지정된 collection에서 찾지 못한 span은 전체 collection의 exact
검색만 허용한다. 따라서 링크 오류를 공식 사전 정본으로 복구할 수는 있지만 관련
없는 약물·처치·해부·응급 collection 전체를 Vector로 재검색하지 않는다.

승인 별칭 release는 PostgreSQL에서 읽지만 승인된 행이 있을 때만 후보에 반영한다.
어떠한 검색 후보도 초안에 자동 확정하지 않는다.

## Configuration ownership

ER:ON backend가 읽는 값:

```text
RECORD_AI_URL=http://clinicalnlp:8765
CLINICAL_RECORD_AI_TIMEOUT_SECONDS=180
```

ClinicalNLP 컨테이너만 읽는 값:

```text
CLINICAL_LLM_PROVIDER=ollama_cloud
OLLAMA_BASE_URL=https://ollama.com
OLLAMA_MODEL=gemma4:31b
OLLAMA_API_KEY=<secret>
CLINICALNLP_DATABASE_URL=postgresql://<user>:<password>@postgres:5432/<db>
CLINICALNLP_UMLS_PYTHON=/runtime/scispacy/.venv/bin/python
CLINICALNLP_UMLS_CACHE_ROOT=/runtime/scispacy/cache
```

`OLLAMA_API_KEY`는 frontend build 환경과 ER:ON backend 환경에 주입하지 않는다.
OCI에서는 Vault secret으로 ClinicalNLP 컨테이너에만 전달한다.

## PostgreSQL release contract

의료사전·KCD·의료 Vector·정책 Vector·승인 별칭은 `clinicalnlp` schema의 versioned
release로 관리한다. source hash가 바뀌면 기존 행을 덮어쓰지 않고 새 release를
적재한 뒤 완전성 검증이 끝난 release만 활성화한다. HTTP 컨테이너의 유일한 파일
마운트는 Linux scispaCy/UMLS runtime이다. SQLite 원본은 서비스에 마운트하지 않고
이관 감사용 보관소에서만 관리한다.

환자 대화, 초안, 후보 선택 이력은 공용 사전·정책 VectorDB에 저장하지 않는다.

## Deployment and exposure

- ClinicalNLP 컨테이너는 Docker Compose의 `clinical` profile에서만 기동한다.
- ClinicalNLP port는 host와 public load balancer에 공개하지 않는다.
- ER:ON backend만 내부 DNS 이름 `clinicalnlp`로 호출한다.
- ClinicalNLP의 outbound는 Ollama Cloud HTTPS와 승인된 아티팩트 다운로드로 제한한다.
- readiness는 사전·VectorDB 무결성과 UMLS worker 상태를 구분해 보고한다.
- Cloud 또는 UMLS 일시 장애가 ER:ON backend 자체의 health를 실패시키지 않는다.

## Data handling gate

개발·평가는 합성 또는 승인된 비식별 데이터만 사용한다. 실환자 식별 가능 정보를
Ollama Cloud로 전송하려면 기관의 법무·개인정보·의료정보보안 검토와 필요한 계약을
먼저 완료한다.

일반 로그에 다음을 남긴다.

```text
request_id, model, prompt/schema/index version, token counts,
stage latency, validation result, bounded error code
```

일반 로그에 API key, STT 원문, prompt 본문, 모델 응답 본문과 환자 식별자를 남기지
않는다.

## Migration sequence

1. 정본 Schema와 프롬프트를 `services/clinicalnlp`에 배치한다.
   완료 기준: 원본 ClinicalNLP와 schema hash가 일치한다.
2. ClinicalNLP 구현과 테스트를 독립 module로 이식한다.
   완료 기준: 합성 fixture와 기존 회귀 테스트가 통과한다.
3. 컨테이너와 외부 asset mount를 추가한다.
   완료 기준: 이미지에 사전·VectorDB·API key가 포함되지 않는다.
4. ER:ON backend adapter와 endpoint를 추가한다.
   완료 기준: 성공·partial·400·502·503·504 계약 테스트가 통과한다.
5. Compose profile과 route 전용 nginx timeout을 추가한다.
   완료 기준: 기본 Compose 실행은 ClinicalNLP 없이 기존과 동일하게 동작한다.
6. frontend의 mock 생성 함수를 실제 draft interface로 교체한다.
   완료 기준: 로딩·partial·오류·편집 상태가 구분되고 초안이 자동 저장되지 않는다.
7. OCI artifact 배포와 Block Volume mount를 검증한다.
   완료 기준: 새 release 전환과 직전 release 롤백 시험이 통과한다.

## Shared-file coordination

다음 파일을 수정하기 직전에 해당 영역 담당자와 작업 시간을 조율한다.

| File | Planned change | Conflict risk |
|---|---|---|
| `backend/app/core/config.py` | `RECORD_AI_URL`, timeout 설정 | medium |
| `backend/app/main.py` | Clinical draft router 등록 | medium |
| `backend/requirements.txt` | backend HTTP client dependency | medium |
| `docker-compose.yml` | opt-in `clinical` profile과 mounts | high |
| `.env.example` | secret 없는 설정 이름 | medium |
| `nginx/conf.d/eron.conf` | draft endpoint 전용 timeout | medium |
| `frontend/src/routes/records.$patientId.tsx` | mock 생성 함수 한 곳 교체 | high |
| `README.md` | 실행·검증 방법 pointer | low |

이번 범위에서는 `database/`, `backend/app/models/`, 기존 record CRUD,
`frontend/src/routeTree.gen.ts`와 API1 저장소를 수정하지 않는다.

## Stage 1 completion criteria

- 이 문서가 Internal ClinicalNLP interface와 ER:ON backend interface를 각각 하나로
  고정한다.
- 요청, 응답, HTTP 상태, timeout, 데이터 처리, VectorDB artifact와 소유권이 모두
  명시돼 있다.
- Ollama Cloud와 OCI 세부 기준은 중복 작성하지 않고 관련 정본 문서를 가리킨다.
- Stage 1 변경은 `docs/` 아래 문서로만 구성되며 실행 코드와 설정을 변경하지 않는다.
