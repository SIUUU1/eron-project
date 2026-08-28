# ER:ON ClinicalNLP — Ollama Cloud / Gemma 4 31B 연동 조사

> 조사 기준일: 2026-08-28 (Asia/Seoul)  
> 범위: Ollama 공식 문서, 공식 모델 라이브러리, 공식 정책, 공식 GitHub 저장소만 사용

## 결론

- 사용자가 말한 "Gemma 31B Cloud"는 현재 실제 제공되는 모델이다.
  - 로컬 Ollama를 Cloud 중계기로 사용할 때: `gemma4:31b-cloud`
  - `ollama.com` Cloud API를 직접 호출할 때: `gemma4:31b`
- ClinicalNLP 서비스에서는 별도 Ollama 데몬이 필요 없는 **직접 Cloud API 방식**을 우선 권장한다.
  - Native Ollama API: `POST https://ollama.com/api/chat`
  - 인증: `Authorization: Bearer ${OLLAMA_API_KEY}`
- Gemma 4 31B의 공식 모델 페이지 표기는 256K context window이며, Cloud 모델은 최대 context로 설정된다고 Ollama가 안내한다.
- 가장 중요한 제약은 **Ollama Cloud가 현재 structured outputs를 지원하지 않는다는 점**이다. JSON Schema를 API에서 강제할 수 있다고 가정하면 안 된다. 프롬프트로 JSON만 요청한 뒤, ClinicalNLP가 Pydantic으로 엄격히 검증하고 제한된 재시도를 수행해야 한다.
- Ollama는 Cloud prompt/response를 요청 처리 중 일시적으로 처리하고, 저장·로그·학습하지 않는다고 밝힌다. 그러나 의료정보 전송에 필요한 HIPAA/BAA, 한국 개인정보보호법·의료법 적합성, 기관별 계약, 확정적인 한국 리전 고정은 검토한 공식 공개 자료에서 확인되지 않았다. **식별 가능한 실제 환자 데이터를 보내기 전 법무·보안·의료정보 책임자의 승인이 필요하다.**

## 1. 모델명 검증

Ollama 공식 [Gemma 4 모델 페이지](https://ollama.com/library/gemma4)는 다음을 명시한다.

- Gemma 4 31B는 Dense 30.7B 모델이다.
- 공식 Cloud 실행 명령은 `ollama run gemma4:31b-cloud`이다.
- 모델 목록에는 `gemma4:31b-cloud`와 `gemma4:cloud`가 Cloud 모델로 표시된다.
- `gemma4:31b`와 `gemma4:31b-cloud` 모두 256K context window로 표시된다.
- 기존 `gemma3` 계열에는 31B가 없고 4B·12B·27B만 있다. 따라서 `gemma3:31b` 또는 일반적인 `gemma:31b`는 이 연동의 올바른 모델명이 아니다.

현재 [Ollama Cloud Native 모델 목록](https://ollama.com/api/tags)과 [OpenAI-compatible 모델 목록](https://ollama.com/v1/models)에는 직접 Cloud API용 모델 ID `gemma4:31b`가 실제로 포함되어 있다.

따라서 설정값을 호출 경로에 따라 분리해야 한다.

| 호출 경로 | Base URL | 모델 ID | 인증 방식 |
|---|---|---|---|
| 직접 Cloud API (권장) | `https://ollama.com/api` | `gemma4:31b` | API key Bearer |
| 로컬 Ollama 중계 | `http://localhost:11434/api` | `gemma4:31b-cloud` | 사전 `ollama signin`; 로컬 호출 자체는 무인증 |
| 직접 OpenAI-compatible API (사전 smoke test 필요) | `https://ollama.com/v1` | `gemma4:31b` | API key Bearer |

Ollama는 Cloud 모델을 때때로 폐기하며 애플리케이션 갱신이 필요할 수 있다고 [Cloud 문서](https://docs.ollama.com/cloud#retirements)에 명시한다. 배포 시 모델 ID를 코드에 하드코딩하지 말고 환경변수로 두며, 시작 시 모델 목록에서 존재 여부를 확인하는 것이 안전하다.

## 2. API와 인증

### 권장: Native Ollama Cloud API 직접 호출

[Ollama API 소개](https://docs.ollama.com/api/introduction)는 Cloud base URL을 `https://ollama.com/api`로, [인증 문서](https://docs.ollama.com/api/authentication)는 API key를 Bearer token으로 보내도록 명시한다. API key는 현재 자동 만료되지 않지만 언제든 철회할 수 있다.

요청 형태는 다음과 같다. 아래는 실제 환자 정보가 아닌 자리표시자만 사용한다.

```http
POST https://ollama.com/api/chat
Authorization: Bearer ${OLLAMA_API_KEY}
Content-Type: application/json

{
  "model": "gemma4:31b",
  "messages": [
    {"role": "system", "content": "응급기록 초안 작성 지침과 출력 계약"},
    {"role": "user", "content": "비식별화된 STT 입력"}
  ],
  "stream": false,
  "options": {
    "temperature": 0.2,
    "num_predict": 4096
  }
}
```

공식 [`POST /api/chat` 문서](https://docs.ollama.com/api/chat)는 `messages`, `stream`, 생성 `options`, 응답의 `done_reason`, 입력·출력 token 수와 duration metric을 제공한다. ClinicalNLP는 최소한 `model`, `done_reason`, `prompt_eval_count`, `eval_count`, `total_duration`을 메타데이터로 남기되 prompt/response 원문과 API key는 로그에 남기지 않아야 한다.

### 대안: 로컬 Ollama 중계

[Cloud 문서](https://docs.ollama.com/cloud#running-cloud-models)에 따라 서버에서 `ollama signin`, `ollama pull gemma4:31b-cloud` 후 로컬 API로 요청할 수 있다. 다만 컨테이너의 `localhost`는 그 컨테이너 자신이므로, Ollama 데몬을 별도 컨테이너로 띄우면 서비스 이름이나 명시적인 네트워크 주소가 필요하다. 또한 서버의 로그인 자격증명 영속화·회전과 Ollama 데몬 운영이 추가된다.

ER:ON 서버 통합에는 API key를 Secret으로 주입할 수 있는 직접 Cloud API가 더 단순하다.

### OpenAI-compatible API

Ollama는 [OpenAI API의 일부와 호환](https://docs.ollama.com/api/openai-compatibility)하며 `/v1/chat/completions`, `/v1/responses`, `/v1/models`를 문서화한다. Cloud 모델도 OpenAI-compatible API에서 동작한다고 [공식 Cloud 모델 안내](https://ollama.com/blog/cloud-models)한다. `/v1/chat/completions`는 `max_tokens`, streaming, JSON mode, tools 등을 호환 필드로 열거한다. 공개된 `https://ollama.com/v1/models`에서도 `gemma4:31b`를 확인할 수 있다.

단, 공식 Cloud 문서의 직접 호출 copy-paste 예시는 Native `/api/chat` 중심이고 OpenAI API는 "일부" 호환이다. 직접 Cloud의 `/v1/chat/completions`는 비식별 합성 입력으로 인증·응답 smoke test를 한 뒤 선택해야 하며, Cloud structured outputs 제한도 그대로 적용된다. 기존 ClinicalNLP가 OpenAI SDK에 강하게 결합되어 있지 않다면 Ollama 고유 응답 metric과 오류 구조를 그대로 받는 Native `/api/chat`이 가장 명확하다.

## 3. JSON/Schema 출력 제약

[Structured Outputs 공식 문서](https://docs.ollama.com/capabilities/structured-outputs)는 첫머리에서 **Ollama Cloud가 현재 structured outputs를 지원하지 않는다**고 명시한다. 로컬 모델에서는 `format: "json"` 또는 JSON Schema를 `format`에 전달할 수 있지만 이 기능을 Cloud 호출에 적용해 계약 준수를 보장하면 안 된다.

응급기록 초안은 다음 방식을 사용해야 한다.

1. system prompt에 허용된 JSON object 구조, 필수·선택 필드, null 처리, 금지 문구를 명시한다.
2. 모델 출력에서 Markdown fence를 허용하지 않고 JSON object 하나만 요청한다.
3. Native API에서는 `think: false`를 명시하고, 그럼에도 생길 수 있는 thought tag나 부가 텍스트를 허용 목록 기반으로 정규화한다. Gemma 4 공식 페이지는 일부 모델이 thinking 비활성 시에도 빈 thought block tag를 생성할 수 있다고 설명한다.
4. 응답 문자열을 JSON으로 parse한다.
5. 기존 `clinical-workflow-v2` Pydantic schema로 엄격히 검증한다.
6. 실패 시 오류 위치와 schema 요약만 포함해 최대 1회 repair 요청한다. 원본 STT 전체를 불필요하게 재전송하지 않는다.
7. 재검증 실패 시 작업을 `failed_validation` 또는 부분 실패로 처리하고 사용자에게 검토 가능한 오류를 표시한다. 잘못된 값을 자동 저장하지 않는다.

즉, `response_format`/`format`을 보조 신호로 보내더라도 **보안 경계나 데이터 계약으로 간주해서는 안 된다.** 로컬 Gemma 4B에서 JSON Schema 강제를 사용했던 코드가 있다면 Cloud 이식 전에 이 의존성을 제거해야 한다.

## 4. Context, 출력 token, timeout과 사용량

### Context

- [Gemma 4 공식 모델 페이지](https://ollama.com/library/gemma4)는 31B 모델의 context length를 256K token으로 표기한다.
- [Context length 문서](https://docs.ollama.com/context-length)는 Cloud 모델이 최대 context length로 설정된다고 안내한다.
- 256K는 입력과 생성 출력 등이 함께 사용하는 모델 context 한도다. STT 원문, system prompt, 사전 검색 근거, 후보 용어, 출력 여유 token의 합을 별도로 예산화해야 한다.
- 응급실 대화 한 건에 256K 전체를 사용하지 말고, 비식별화·중복 제거·근거 상위 K개 제한으로 최소 필요 정보만 전송한다.

### 출력 token

- Native Ollama 호출은 생성 option으로 `num_predict`를 사용하고, OpenAI-compatible `/v1/chat/completions`는 `max_tokens`를 지원한다.
- Ollama 공식 공개 자료에서 `gemma4:31b` Cloud의 별도 고정 최대 출력 token 수는 확인되지 않았다. 따라서 애플리케이션에서 명시적인 출력 예산을 두고, 응답의 `done_reason`과 `eval_count`를 검사해 잘림을 탐지해야 한다.
- 초안 필드 수와 근거 배열 크기를 제한하고, 시작값은 실제 계약을 기준으로 부하·완결성 시험 후 정한다. 문서 예시의 4096은 구현 시작값일 뿐 공급자 보장치가 아니다.

### Timeout, retry, rate limit

- 공식 Cloud/API 문서에는 Cloud 요청의 고정 server timeout이나 응답시간 SLA가 공개되어 있지 않다.
- 공식 Python client는 추가 인자를 `httpx.Client`에 전달하며, 현재 source의 기본 timeout은 `None`이다. 따라서 무기한 대기하지 않도록 ClinicalNLP에서 connect/read/total deadline을 명시해야 한다. [공식 Python client README](https://github.com/ollama/ollama-python#custom-client), [client source](https://github.com/ollama/ollama-python/blob/main/ollama/_client.py)
- [오류 문서](https://docs.ollama.com/api/errors)는 `429`를 rate limit 초과, `502`를 Cloud 모델 연결 실패의 예로 든다.
- 재시도는 connect 오류, `429`, 일시적인 `5xx`에만 제한된 횟수로 exponential backoff와 jitter를 적용한다. `400`, schema validation 실패, 임상 입력 오류를 무조건 재시도하지 않는다.
- 동일 STT에 대한 중복 초안을 막는 idempotency/job 상태가 필요하다. 장시간 요청은 웹 요청 안에서 동기 처리하기보다 기존 비동기 초안 작업으로 감싼다.
- [Ollama 요금/사용량 문서](https://ollama.com/pricing#usage)는 개인 요금제 사용량이 모델과 input/cached-input/output token에 따라 계산되고, 세션 한도는 5시간마다, 주간 한도는 7일마다 초기화된다고 설명한다. 정확한 고정 token quota는 공개하지 않는다.

초기 운영값은 부하 시험으로 확정하되 다음을 환경변수화하는 편이 좋다.

```env
CLINICAL_LLM_PROVIDER=ollama_cloud
OLLAMA_BASE_URL=https://ollama.com
OLLAMA_MODEL=gemma4:31b
OLLAMA_CONNECT_TIMEOUT_SECONDS=10
OLLAMA_READ_TIMEOUT_SECONDS=300
OLLAMA_MAX_OUTPUT_TOKENS=4096
OLLAMA_MAX_RETRIES=2
# OLLAMA_API_KEY는 Secret 저장소에서만 주입
```

위 수치는 공급자 공식 제한이 아니라 ER:ON 측 운영 시작값이다. 실제 ClinicalNLP 합성 데이터 평가에서 p95 처리시간, 잘림률, validation 재시도율을 측정해 조정해야 한다.

## 5. 의료·개인정보 처리 판단

Ollama [Privacy Policy](https://ollama.com/privacy)(2026년 3월 갱신)와 [FAQ](https://docs.ollama.com/faq#does-ollama-send-my-prompts-and-answers-back-to-ollamacom)는 다음을 밝힌다.

- 로컬 실행 데이터는 Ollama가 수집·저장·전송하거나 접근하지 않는다.
- Cloud 사용 시 prompt와 response를 서비스 제공을 위해 일시적으로 처리하며, 요청 이행에 필요한 시간 이후 저장하지 않는다.
- prompt/response content를 로그하거나 AI 모델 학습에 사용하지 않는다.
- prompt/response content를 포함하지 않는 제한적인 계정·기기·사용량·진단 metadata는 수집할 수 있다.
- 서비스 제공을 위해 cloud infrastructure provider와 model inference provider가 처리에 참여할 수 있다.
- 데이터가 미국에서 이전·처리될 수 있다고 Privacy Policy에 명시되어 있고, [Pricing FAQ](https://ollama.com/pricing#models)는 모델과 compute가 주로 미국에 있으며 수요에 따라 유럽·싱가포르로 routing될 수 있다고 설명한다.

[Terms of Service](https://ollama.com/terms)는 입력·출력의 소유권을 사용자에게 두고 서비스 제공만을 위한 제한적 처리 license를 요구하며, AI 출력이 부정확·불완전할 수 있으므로 중요한 결정 전에 독립 검증해야 한다고 명시한다. 이는 ER:ON의 결과를 **의료진 검토 전 초안**으로만 취급해야 한다는 프로젝트 원칙과 일치한다.

### Cloud 전송 전 필수 확인

검토한 Ollama 공식 공개 자료에서는 다음을 확인하지 못했다.

- 의료정보 처리에 관한 HIPAA 적합성 또는 BAA 제공 여부
- 한국 개인정보보호법·의료법 및 국외이전 요구에 대한 별도 약정
- 고객이 선택하고 고정할 수 있는 한국 리전 또는 특정 리전 고정 기능
- 개인 요금제에 대한 DPA, 보안 인증, 사고 통지 SLA의 구체 조건

따라서 공개 Privacy Policy의 "저장·로그·학습하지 않음"만으로 실제 환자의 식별 가능한 의료정보를 전송해도 된다고 결론 내리면 안 된다. 운영 전 다음이 필요하다.

1. 법무·개인정보보호·의료정보보안 책임자가 국외이전, 위탁처리, 고지/동의 또는 다른 적법 근거를 검토한다.
2. Ollama에 의료정보 취급, DPA/BAA 가능 여부, subprocessors, region 고정, 사고 통지, 삭제 검증을 서면 확인한다.
3. 계약 검토가 끝날 때까지 합성 데이터와 비식별 데이터만 사용한다.
4. 이름, 주민등록번호, 연락처, 상세 주소, 병원 내부 식별자 등 직접 식별자를 Cloud 전송 전에 제거·치환한다.
5. STT 원문 전체 대신 초안 생성에 필요한 최소 범위만 보내고, request/response body logging과 APM payload capture를 비활성화한다.
6. API key는 frontend에 노출하지 않고 ClinicalNLP 서버 Secret으로만 주입하며, 폐기·회전 절차를 둔다.

Ollama는 [요금제 안내](https://ollama.com/pricing)에서 Team 요금제에 "zero data retention and logging", Enterprise에 custom terms 및 security/procurement support를 표시한다. 실제 의료 운영을 고려한다면 개인 계정보다 기관용 계약 가능성을 먼저 확인해야 한다.

## 6. ClinicalNLP 이식 시 필요한 변경

### LLM adapter

- 로컬 llama.cpp/Gemma 4B 호출 코드를 `ClinicalLlmClient` 같은 interface 뒤로 격리한다.
- 구현체를 `LocalGemmaClient`와 `OllamaCloudClient`로 분리해 평가 기간에 같은 fixture로 비교할 수 있게 한다.
- Base URL, model, timeout, token budget, retry 수는 환경변수로 받고 API key는 Secret으로만 주입한다.
- 서비스 시작 시 `/api/tags` 또는 `/v1/models`로 `gemma4:31b` 가용성을 확인하되, 일시 장애가 전체 서비스 부팅 실패로 확산되지 않도록 readiness 상태로 분리한다.

### 출력 계약

- 기존 `clinical-workflow-v2` Pydantic schema를 API 응답의 단일 source of truth로 유지한다.
- prompt-only JSON, JSON parse, strict validation, 최대 1회 repair, 최종 실패의 상태 전이를 구현한다.
- hallucination 방지를 위해 각 초안 필드가 STT evidence/span을 참조하도록 하고 근거 없는 진단 확정을 허용하지 않는다.
- 모델명, prompt version, schema version, input/output token, latency, validation 결과는 남기되 환자 원문과 생성 원문은 일반 로그에서 제외한다.

### 안정성

- 비동기 job으로 실행하고 중복 생성 방지 key를 둔다.
- `429`·일시 `5xx`에 제한적인 retry/backoff, circuit breaker와 사용량 고갈 메시지를 둔다.
- timeout 뒤 요청 결과가 불명확할 수 있으므로 같은 방문 기록에 중복 결과를 자동 반영하지 않는다.
- Cloud 모델 retirement에 대비해 모델 ID 변경을 배포 없이 가능하게 하고, 변경 전 합성 회귀평가를 필수화한다.

### 검증

- 기존 로컬 Gemma 4B gold/synthetic fixture로 필드 완결성, evidence 일치율, 용어 정확도, JSON parse·schema 통과율을 비교한다.
- 긴 STT, 빈 STT, 중복 대화, 한국어/영어 혼합, 모순된 환자 진술, 민감정보 마스킹을 시험한다.
- `401/403`, `404` 모델 폐기, `429`, `502`, read timeout, 잘린 JSON, schema 불일치, repair 실패를 강제하는 adapter 단위 테스트를 둔다.
- 실제 환자 데이터가 아닌 합성 입력으로 Cloud smoke test를 수행한다.

## 7. 도입 게이트

| 게이트 | 완료 기준 |
|---|---|
| 모델/API | `gemma4:31b` 직접 Cloud 호출 성공, 모델 가용성 확인 |
| 계약 | JSON parse + `clinical-workflow-v2` strict validation + 제한적 repair 통과 |
| 품질 | 합성/비식별 평가에서 기존 4B 대비 사전 합의한 품질 기준 충족 |
| 안정성 | timeout, 429, 5xx, 모델 폐기, 사용량 고갈 처리 검증 |
| 보안 | key 관리, payload logging 차단, 비식별화, 최소 전송 검증 |
| 법무/개인정보 | 국외이전·위탁·계약/DPA 및 실제 의료정보 사용 승인 완료 |
| 의료 안전 | 모든 결과를 의료진 검토 전 초안으로 표시하고 자동 확정 금지 |

이 게이트 중 개인정보·계약 검토가 완료되지 않았다면 개발·평가는 합성 또는 적절히 비식별화된 데이터로만 제한한다.

## 공식 출처

- [Gemma 4 모델 페이지](https://ollama.com/library/gemma4)
- [Ollama Cloud 문서](https://docs.ollama.com/cloud)
- [API 소개와 Cloud base URL](https://docs.ollama.com/api/introduction)
- [API 인증](https://docs.ollama.com/api/authentication)
- [`POST /api/chat`](https://docs.ollama.com/api/chat)
- [Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs)
- [OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility)
- [Context length](https://docs.ollama.com/context-length)
- [API usage metrics](https://docs.ollama.com/api/usage)
- [API errors](https://docs.ollama.com/api/errors)
- [Cloud Native 모델 목록](https://ollama.com/api/tags)
- [OpenAI-compatible 모델 목록](https://ollama.com/v1/models)
- [Pricing / usage / hosting FAQ](https://ollama.com/pricing)
- [Privacy Policy](https://ollama.com/privacy)
- [Terms of Service](https://ollama.com/terms)
- [Ollama Python 공식 저장소](https://github.com/ollama/ollama-python)
