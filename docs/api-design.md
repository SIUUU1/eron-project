# ER:ON — API 설계

> 상태: **구현 완료**. 아래 계약은 실제로 동작하는 API 를 반영합니다.
> 작성 2026-08-26 / 개정 2026-08-26 (rev.3 — 구현 결과 반영)
> 진입점: `http://localhost:8080` (nginx) · Swagger 직접 접근: `http://localhost:8100/docs`
>
> ⚠ 본문은 **rev.3(2026-08-26) 시점의 8개 엔드포인트 계약**입니다. 그 이후 기록 영역과
> 예측 실행 API 가 추가되어 현재는 33개입니다. 추가분은 §0-2 에 모아 두었고,
> **항상 최신인 정본은 `/openapi.json`** 입니다 (운영: `https://eron.co.kr/openapi.json`).
> 배포 주소는 `docs/oci-deployment.md` 를 참고하세요.

---

## 0. 이번 개정에서 바뀐 것

| # | 변경 |
|---|---|
| 1 | **기록 영역 API 4종 삭제** — `/api/ed/records/incomplete`, `GET·PUT /api/ed/stays/{id}/record`, `/api/ed/stays/{id}/diagnoses` |
| 2 | `dashboard/summary`에서 `incomplete_records` 필드 삭제 → 대시보드 "기록 미완료" 카드는 **mock 유지** |
| 3 | `/api/ed/stays` 응답에서 `record_status` 필드 삭제 → 목록 "기록 상태" 컬럼은 **mock 유지** |
| 4 | **엔드포인트 12개 → 8개** |
| 5 | nginx 리버스 프록시 도입에 따라 **동일 오리진 + 상대 경로** 호출로 전환, CORS는 로컬 dev 전용으로 축소 |

### rev.3 — 구현하며 설계에서 바뀐 것

| 항목 | 설계안 | 구현 | 사유 |
|---|---|---|---|
| `meta.model_connected` | `PREDICT_AI_URL` 설정 여부 | **예측 데이터 존재 여부** (`EXISTS(app.prediction)`) | URL 만 설정돼 있고 호출부가 없어 `true` 가 되는 게 오해를 부름. 프론트가 알아야 할 건 "위험도를 표시할 수 있는가" |
| `dashboard/summary` | 위험도 4단계만 | **`unassessed` 필드 추가** | 예측이 0건일 때 300명이 어디로 갔는지 드러나야 함 |
| 병상 색상 | 예측 `risk_level` | 예측이 없으면 **triage acuity 로 대체**, `meta.status_source` 로 근거 표시 | 예측 0건에서 전 병상이 같은 색이 되어 화면이 무의미해짐. 대체 근거도 실데이터(ESI) |
| 재평가 우선순위 | 예측 확률 순 | 예측이 없으면 **acuity 순** (동일 플래그) | 위와 동일 |
| `mimic` FK | 테이블 DDL 에 인라인 | **적재 후 `03_constraints.sql` 로 부여** | COPY 적재 순서 의존을 없애면서 무결성은 유지 |

**기록 영역은 어떤 형태로도 건드리지 않습니다.** `/records`, `/records/$patientId` 화면, `mock-data.ts`의 기록 관련 export, 기존 `backend/app/api/records.py`(별개 도메인) 모두 그대로 둡니다.

---

## 0-2. rev.3 이후 추가된 엔드포인트 (2026-09-02 기준)

본문 §1 이후의 계약은 rev.3 당시 8개를 기준으로 쓰였습니다. 아래는 그 뒤에 추가된
것들로, 상세 스키마는 `/openapi.json` 과 각 라우터를 정본으로 봅니다.

### 응급진료기록 (기록 영역 — rev.3 에서는 범위 밖이었다)

| 메서드 | 경로 | 용도 |
|---|---|---|
| GET · PUT | `/api/clinical-records/by-stay/{ed_stay_id}` | DRAFT/SIGNED 복원 · 반복 임시저장 |
| POST | `/api/clinical-records/{record_id}/sign` | 최신 DRAFT 를 SIGNED 로 전환 |
| POST | `/api/clinical-records/draft` | 대화 기록 → 초안 생성 (ClinicalNLP) |
| POST | `/api/clinical-records/transcribe` | 음성 → Whisper 전사 |
| POST | `/api/clinical-records/draft/audio` | 전사 + 초안 생성 통합 경로 (호환 유지) |

저장 규칙과 상태 전이는 `docs/clinical-record-persistence.md` 를 따릅니다.
이 세 POST 는 nginx 에서 각각 630s · 310s · 930s 의 proxy timeout 을 받습니다
(`nginx/conf.d/eron-proxy.inc`). backend deadline 이 먼저 구조화된 504 를 반환하도록
둔 여유이므로 임의로 줄이지 않습니다.

### 진단코드

| 메서드 | 경로 | 용도 |
|---|---|---|
| GET | `/api/kcd/search` | KCD 코드 검색 |

> 현재 `public.kcd_codes` 가 비어 있고 약어 확장용 사전 자산도 배포되어 있지 않아
> 빈 결과를 반환합니다. 500 이 아니라 `{"items": [], "total": 0}` 으로 degrade 합니다.

### 경고 확인

| 메서드 | 경로 | 용도 |
|---|---|---|
| POST | `/api/ed/alerts/{stay_id}/acknowledge` | 의료진 재검토 완료 표시 (`app.prediction_ack`) |

### 기존 CRUD (rev.3 본문에서 다루지 않은 자체 도메인)

`CLAUDE.md` 의 "기존 route prefix·응답 형태 유지" 규칙에 따라 그대로 유지되는
`/api/patients` · `/api/visits` · `/api/vitals` · `/api/predictions` · `/api/records`
계열입니다. §6 에서 설명한 대로 `/api/ed/*` 와는 **다른 자원**입니다.

| 메서드 | 경로 |
|---|---|
| GET, PUT, DELETE | `/api/patients/{patient_id}` |
| GET, POST | `/api/visits` |
| GET, PUT, DELETE | `/api/visits/{visit_id}` |
| GET | `/api/visits/patient/{patient_id}` |
| GET, POST | `/api/visits/{visit_id}/vitals` · `/predictions` · `/records` |
| GET, PUT, DELETE | `/api/vitals/{vital_id}` · `/api/records/{record_id}` |
| GET | `/api/predictions/{prediction_id}` |

---

## 1. 설계 원칙

1. **프론트엔드 코드에서 역산.** 화면이 실제로 쓰는 필드만 정의합니다. 임의 엔드포인트를 만들지 않습니다.
2. **기존 계약 보존.** 기존 `/api/patients`, `/api/visits`, `/api/vitals`, `/api/predictions`, `/api/records`는 그대로 둡니다. MIMIC 기반 조회는 **`/api/ed/*` 신규 네임스페이스**를 사용합니다 (architecture.md §6).
3. **읽기 전용.** `/api/ed/*`는 원칙적으로 **GET만** 제공합니다. 유일한 예외는 `/api/ed/demo/*` 로, `app.demo_clock` 한 행만 씁니다. MIMIC 원천 데이터는 어떤 경로로도 변경하지 않습니다.
4. **필요 컬럼만.** `SELECT *` 및 전체 스캔을 발생시키는 엔드포인트를 만들지 않습니다.
5. **미확정은 null + 문서화.** MIMIC에 없는 값은 지어내지 않고 `null`로 반환하며, 필드 설명에 사유를 적습니다 (R4).
6. **범위 밖은 아예 만들지 않는다.** 기록 관련 필드를 "빈 값으로라도" 내려보내지 않습니다. 프론트가 mock을 쓰는지 API를 쓰는지 한눈에 구분되게 합니다.

---

## 2. 엔드포인트 목록 (8종)

| # | Method | Path | 사용 화면 |
|---|---|---|---|
| 1 | GET | `/api/ed/dashboard/summary` | `/` 요약 카드 · `app-header` 배지 |
| 2 | GET | `/api/ed/dashboard/beds` | `/` 병상 현황판 |
| 3 | GET | `/api/ed/alerts` | `/` 실시간 AI 경고 |
| 4 | GET | `/api/ed/reassess-queue` | `/` 위험 환자 우선순위 |
| 5 | GET | `/api/ed/stays` | `/monitoring` 환자 목록 |
| 6 | GET | `/api/ed/stays/{stay_id}` | `/monitoring/$patientId` 헤더 |
| 7 | GET | `/api/ed/stays/{stay_id}/vitals` | 상세 — 현재 Vital + 시간별 추이 |
| 8 | GET | `/api/ed/stays/{stay_id}/predictions` | 상세 — AI 분석 · 확률 추이 |
| 9 | GET | `/api/ed/demo/clock` | 헤더 — 데모 시계 상태 |
| 10 | POST | `/api/ed/demo/advance?hours=` | 헤더 — 시각 진행/되감기 (`+1`/`-1`). 되감기는 시나리오 시작점이 하한 |
| 11 | POST | `/api/ed/demo/speed?value=` | 헤더 — 배속 (0=정지, 1=실시간, 3600=1초에 1시간) |
| 12 | POST | `/api/ed/demo/reset` | 헤더 — 시나리오 처음으로 |
| — | — | `/api/patients` 외 기존 5종 | (프론트 미사용) **현행 유지** |
| — | GET | `/health`, `/health/db` | 헬스체크 **현행 유지** |

### ⛔ 삭제된 엔드포인트 (기록 영역)

```text
GET  /api/ed/records/incomplete          → 대시보드 "기록 미완료 알림"은 mock 유지
GET  /api/ed/stays/{stay_id}/record      → 기록 화면 미변경
PUT  /api/ed/stays/{stay_id}/record      → 기록 화면 미변경
GET  /api/ed/stays/{stay_id}/diagnoses   → 진단코드 추천은 기록 화면 기능
```

> `mimic.ed_diagnosis`는 향후 모델 feature 후보로 **DB에는 적재하되 API로 노출하지 않습니다** (database-design.md §7.5).

---

## 3. 공통 규약

### 3.1 요청 경로

프론트엔드는 **상대 경로**로 호출합니다. nginx가 `/api/`를 backend로 프록시합니다.

```text
Browser  GET /api/ed/stays        (오리진 localhost:8080)
   → nginx  location /api/  → backend:8000
```

`VITE_API_BASE_URL`은 **빈 값을 기본**으로 하며, 컨테이너 밖 `vite dev`에서만 `http://localhost:8100`을 지정합니다 (architecture.md §8).

### 3.2 페이지네이션

```json
{ "items": [], "page": 1, "page_size": 20, "total": 300 }
```

`page` ≥ 1 (기본 1), `page_size` 1–100 (기본 20).

### 3.3 오류 응답

FastAPI 기본 형태를 유지합니다.

```json
{ "detail": "ED stay not found" }
```

| 코드 | 상황 |
|---|---|
| 400 | 잘못된 파라미터 (`page_size` 범위 초과, 알 수 없는 `risk_level` 등) |
| 404 | `stay_id`가 코호트에 없음 |
| 422 | FastAPI 타입 검증 실패 (자동) |
| 500 | DB 연결 실패 등 내부 오류 |
| 502 / 504 | **nginx가 생성.** backend 미기동·타임아웃 시 FastAPI가 아니라 nginx의 HTML 오류 페이지가 반환됩니다 |

> **프론트 주의**: 502/504는 `detail` JSON이 아닙니다. API 클라이언트에서 `res.json()` 파싱 실패를 반드시 처리해야 합니다 (§6).

### 3.4 단위 · 표기 규약

| 항목 | 규약 |
|---|---|
| 체온 | API는 **℃로 변환하여 반환** (`temperature_c`). DB는 원본 °F 보관 |
| 시각 | ISO 8601 문자열. 데모 시간축 적용값 |
| 결측 | `null` (0으로 대체하지 않음) |
| 확률 | `0.0–1.0` (백분율 변환은 프론트 담당) |
| 성별 | `"M" \| "F"` 원본 그대로. 한글 표기는 프론트 매핑 |

### 3.5 데이터 출처 표시

모든 최상위 응답에 `meta`를 포함합니다.

```json
{ "meta": { "is_demo_timeline": true, "data_source": "mimic-iv-ed", "cohort_size": 300 } }
```

`CLAUDE.md`의 "mock과 live를 한 흐름에서 섞지 않는다" 규칙에 대응하는 장치입니다.
기록 관련 값은 API에 아예 포함되지 않으므로, **API 응답에 있으면 live, 없으면 mock**이라는 단순한 규칙이 성립합니다.

---

## 4. 엔드포인트 상세

### 4.1 `GET /api/ed/dashboard/summary`

**화면**: `/` 요약 카드 + `app-header.tsx` 상단 배지

```json
{
  "total": 300,
  "critical": 12,
  "rising": 31,
  "watch": 88,
  "stable": 169,
  "ai_alerts_today": 0,
  "meta": { "is_demo_timeline": true, "data_source": "mimic-iv-ed" }
}
```

| 필드 | 산출 |
|---|---|
| `total` | **현재 재실 중**인 환자 수 (`NOT has_departed`). 퇴실자는 제외 |
| `discharged` | 코호트 중 이미 퇴실한 환자 수 |
| `critical`/`rising`/`watch`/`stable` | `app.v_latest_prediction.risk_level` 집계. **예측 없으면 전부 0** |
| `ai_alerts_today` | 데모 시계 기준 **오늘 경보가 켜진 건수**. `app.prediction` 에서 파생하며 `/api/ed/alerts` 와 같은 정의다. 예측이 없으면 0 |

> ⛔ **`incomplete_records`는 제공하지 않습니다.** 대시보드의 "기록 미완료" 카드(`summary.incompleteRecords`)는 mock을 계속 사용합니다.

---

### 4.2 `GET /api/ed/dashboard/beds`

**화면**: `/` 병상 현황판 (`bedZones`, `bedSummary`)
**전제**: D2 확정 — 병상 배치·장비는 MIMIC에 없는 **데모 배정**입니다.

```json
{
  "summary": { "total": 36, "critical": 6, "moderate": 12, "low": 10, "empty": 8 },
  "zones": [
    {
      "zone": "A 구역 (Resus)",
      "beds": [
        { "bed_id": "A01", "status": "critical", "stay_id": "33258284",
          "display_name": "환자 A01", "age": 72, "sex": "M",
          "devices": ["E", "V", "C"] },
        { "bed_id": "A06", "status": "empty", "stay_id": null,
          "display_name": null, "age": null, "sex": null, "devices": [] }
      ]
    }
  ],
  "meta": { "is_demo_assignment": true }
}
```

`status` 파생: `critical→critical`, `rising→moderate`, `watch|stable→low`, 미배정 → `empty`.
`devices`(E/V/C)는 **MIMIC에 없습니다.** Phase B에서 `procedureevents`(삽관)·`inputevents`(승압제)로 근사 가능하나, 현재는 데모 값입니다.

---

### 4.3 `GET /api/ed/alerts`

**화면**: `/` 실시간 AI 경고

| Query | 기본 | 설명 |
|---|---|---|
| `limit` | 20 | 1–100 |
| `since` | — | ISO 8601 |

```json
{
  "items": [
    { "id": 414, "stay_id": "35716124", "display_name": "이**",
      "alert_time": "2026-09-01T15:53:58", "level": "watch",
      "risk_probability": 0.0592,
      "message": "호흡수 18회/분 → 26회/분 · 심박수 108 bpm → 111 bpm",
      "reason_type": "risk_increase_signal",
      "acknowledged_at": null }
  ],
  "meta": { "model_connected": true }
}
```

**경고 정의 (2026-09-01 연동):**

- `app.alert` 는 **비어 있고 적재하지 않습니다.** 경고는 `app.prediction` 에서 파생합니다.
  예측은 매 주기 재계산·upsert 되므로 경고를 따로 쌓으면 두 곳이 어긋납니다.
- 1건 = **경보가 꺼져 있다가 켜진 시점**(`detail.alarm` false→true). 시점마다 세면 한
  환자가 매시간 경고를 냅니다.
- `alert_time` 은 **데모 시간축**이며, 아직 도래하지 않은 예측 시점은 감춥니다.
  `since` 도 같은 축으로 비교합니다.
- `message` 는 riskmodel 이 만든 기여 신호 문장입니다(§4.8). 설명이 없으면
  `"모델 경보 임계값 초과"` 가 들어갑니다. **악화의 원인이 아닙니다.**
- `acknowledged_at` 은 **항상 null** — 파생 목록이라 확인 이력을 저장할 곳이 없습니다.
  확인 이력이 필요해지면 `app.alert` 적재로 전환해야 합니다.
- `band` 로 구간을 거를 수 있습니다(`green|amber|red`). 예전 `level`(4단계) 파라미터는
  화면 표기와 경계가 어긋나서 제거했습니다.

**🎨 화면 등급 체계 — 모델 3구간으로 통일 (2026-09-01).**

| band | 화면 표기 | 경계(보정 확률) |
|---|---|---|
| `red` | 🔴 재평가 필요 | ≥ 13.34% |
| `amber` | 🟡 관찰 필요 | 3.58% ~ 13.34% |
| `green` | 🟢 저위험 | < 3.58% |

경계는 `artifacts/bundle.json` 의 `risk_bands` 실측값입니다. 환자 목록·환자 상세·병상
현황판·실시간 AI 경고·위험 환자 우선순위가 모두 이 3구간을 씁니다. 응답에는
`risk_band`(목록·상세·재평가 큐) 또는 `band`(경보)로 실려 갑니다.

⚠ `risk_level`(4단계: stable/watch/rising/critical)은 `.env` 의 `RISK_*` 경계이며 여전히
   응답에 남아 있지만 **화면 배지에는 쓰지 않습니다.** 두 체계를 섞으면 배지 문구와 실제
   필터 기준이 어긋납니다(실제로 경보 카드에서 "재평가 필요" 배지에 40% 필터가 걸려 있었습니다).

> 예측이 없으면 `items: []`, `model_connected: false` 입니다. 프론트는 빈 상태 UI를 표시하며, 가짜 경고를 생성하지 않습니다 (R4).

---

### 4.4 `GET /api/ed/reassess-queue`

**화면**: `/` 위험 환자 우선순위

```json
{
  "items": [
    { "stay_id": "33258284", "display_name": "환자 A01",
      "risk_level": "critical", "risk_probability": 0.87,
      "due_minutes": 0, "due_label": "즉시" }
  ]
}
```

`due_minutes` 파생 규칙 (`settings.tsx`의 임계값 UI와 정합):

| risk_level | due_minutes | due_label |
|---|---:|---|
| critical | 0 | "즉시" |
| rising | 10 | "10분 내" |
| watch | 30 | "30분 내" |
| stable | — | 목록 제외 |

정렬: `risk_probability DESC`. 예측이 없으면 빈 배열입니다.

---

### 4.5 `GET /api/ed/stays` — 환자 목록

**화면**: `/monitoring` (`PatientListTable`)

> `/records` 화면도 같은 `PatientListTable`을 재사용하지만, **기록 영역은 범위 밖이므로 해당 화면은 mock을 계속 사용**합니다. 컴포넌트 분기는 구현 단계에서 `base` prop으로 처리합니다 (UI 변경 없음, R6).

| Query | 기본 | 설명 |
|---|---|---|
| `page` | 1 | ≥ 1 |
| `page_size` | 20 | 1–100 |
| `risk_level` | — | `critical\|rising\|watch\|stable` |
| `acuity` | — | 1–5 (ESI) |
| `search` | — | `stay_id` 접두 일치 또는 `chiefcomplaint` 부분 일치 |
| `sort` | `risk` | `risk`(위험도→확률 desc) \| `arrival`(내원시간 desc)<br>**`/monitoring` 화면은 `arrival` 을 사용한다** (모델 미연동 상태에서 위험도 정렬이 무의미하므로) |

`sort=risk`는 프론트 `riskOrder` + `deteriorationProbability desc`와 동일한 순서를 재현합니다.

```json
{
  "items": [
    {
      "stay_id": "33258284",
      "display_name": "환자 A01",
      "sex": "M",
      "age": 72,
      "arrived_at": "2026-08-26T08:35:00",
      "acuity": 2,
      "chief_complaint": "Abd pain",
      "chief_complaint_detail": "Abd pain, Abdominal distention",
      "risk_level": "critical",
      "risk_probability": 0.87,
      "latest_vital": {
        "measured_at": "2026-08-26T12:40:00",
        "heart_rate": 118, "resp_rate": 26,
        "sbp": 88, "dbp": 56,
        "spo2": 91, "temperature_c": 37.8,
        "consciousness": null
      },
      "bed_id": "A01"
    }
  ],
  "page": 1, "page_size": 20, "total": 300,
  "meta": { "is_demo_timeline": true }
}
```

**프론트 `Patient` 타입 대비 차이:**

| 프론트 필드 | API | 비고 |
|---|---|---|
| `id` | `stay_id` | string |
| `name` | `display_name` | **성씨 마스킹 가명** `김**` (D1). 중복 가능 — 식별은 `stay_id` |
| `ktas` | `acuity` | **ESI**. UI 라벨은 유지, 의미는 각주 |
| `deteriorationProbability` (0–100) | `risk_probability` (0–1) | 프론트에서 ×100 |
| `vitals.bt` | `latest_vital.temperature_c` | °F→℃ 변환 완료 |
| `vitals.mental` | `latest_vital.consciousness` | **항상 `null`.** ED 테이블에 의식수준 없음 (TODO) |
| `bed` | `bed_id` | 데모 배정 (D2 확정). **퇴실한 환자의 병상은 비어 있는 것으로 판정** |
| — | `discharge_type` | **신규.** `icu`\|`admitted`\|`home`\|`expired`, 재실 중이면 `null` → 화면 "퇴실" 컬럼 |
| — | `departed_at` | **신규.** 퇴실 시각(데모 시간축). 재실 중이면 `null` |
| `recordStatus` | **미제공** | ⛔ 기록 영역 범위 밖 → **mock 유지** |

**쿼리 구현**: `edstays ⋈ triage ⋈ patients ⋈ v_latest_vitalsign ⋈ v_latest_prediction ⋈ patient_alias ⋈ bed_assignment`.
LATERAL 서브쿼리로 stay당 최신 1건씩만 읽습니다. **N+1을 만들지 않습니다.**

---

### 4.6 `GET /api/ed/stays/{stay_id}` — 환자 상세

**화면**: `/monitoring/$patientId` 헤더

```json
{
  "stay_id": "33258284",
  "subject_id_masked": "1000****",
  "display_name": "환자 A01",
  "sex": "M",
  "age": 72,
  "race": "WHITE",
  "arrived_at": "2026-08-26T08:35:00",
  "departed_at": null,
  "arrival_transport": "AMBULANCE",
  "arrival_route": "TRANSFER FROM HOSPITAL",
  "acuity": 2,
  "chief_complaint": "Abd pain",
  "chief_complaint_detail": "Abd pain, Abdominal distention",
  "triage": {
    "heart_rate": 70, "resp_rate": 16, "sbp": 106, "dbp": 63,
    "spo2": 97, "temperature_c": 36.9, "pain": "0"
  },
  "disposition": "ADMITTED",
  "hospital": { "hadm_id": "22595853", "admitted": true, "icu_transferred": true },
  "risk_level": "critical",
  "risk_probability": 0.87,
  "bed_id": "A01",
  "meta": { "is_demo_timeline": true }
}
```

| 필드 | 주의 |
|---|---|
| `subject_id_masked` | 원본 `subject_id`를 그대로 노출하지 않습니다 |
| `arrival_route` | `admissions.admission_location`. **hadm_id 없으면 `null`** (전체의 52.2%) |
| `triage.pain` | **문자열**. `"unable"`, `"uta"` 등 비수치 값 존재 |
| `hospital.icu_transferred` | `icustays`에 해당 `hadm_id` 존재 여부. 악화 결과 확인용 |
| `record_status` | ⛔ **미제공** (기록 영역 범위 밖) |

**404**: 코호트에 없는 `stay_id`.

> `/records/$patientId` 화면도 환자 기본 정보를 표시하지만, 기록 영역 미변경 방침에 따라 **해당 화면은 이 API를 호출하지 않고 mock을 유지**합니다.

---

### 4.7 `GET /api/ed/stays/{stay_id}/vitals`

**화면**: 상세 "현재 Vital" 카드 + "시간별 상태 변화" 차트/표

| Query | 기본 | 설명 |
|---|---|---|
| `limit` | 100 | 1–500 |
| `order` | `asc` | `asc`(차트) \| `desc` |

```json
{
  "stay_id": "33258284",
  "vitals": [
    { "measured_at": "2026-08-26T09:00:00",
      "heart_rate": 92, "resp_rate": 18,
      "sbp": 132, "dbp": 84, "spo2": 97,
      "temperature_c": 36.8, "rhythm": null, "pain": "7",
      "consciousness": null }
  ],
  "latest": { "...": "vitals의 마지막 요소와 동일" },
  "count": 8,
  "meta": { "outlier_filtered": true, "temperature_unit": "celsius" }
}
```

- `outlier_filtered: true` — `mimic.v_ed_vitalsign_clean`의 범위 밖 값은 `null` 처리됩니다 (database-design.md §11).
- **결측이 많습니다**: `temperature` 36.1%, `spo2` 8.7%, `sbp`/`dbp` 5.2% NULL. 차트에서 선 끊김 처리가 필요합니다.
- `consciousness`는 **항상 `null`** (TODO — ED 테이블에 없음).

---

### 4.8 `GET /api/ed/stays/{stay_id}/predictions`

**화면**: 상세 "AI 분석" 카드 + 차트의 `probability` 시리즈

```json
{
  "stay_id": "31660580",
  "predictions": [
    { "prediction_time": "2026-09-01T16:51:24",
      "t_idx": 5, "horizon_minutes": 180,
      "risk_probability": 0.7352, "risk_level": "critical",
      "model_version": "2.0.0" }
  ],
  "latest": {
    "risk_probability": 0.7352,
    "risk_level": "critical",
    "risk_factors": ["초기 Triage Acuity 1", "수축기 혈압 최저 84 mmHg"],
    "risk_signals": [
      { "feature": "triage_acuity", "feature_label": "초기 Triage Acuity",
        "text": "초기 Triage Acuity 1", "value": 1.0,
        "contribution": 0.7503, "contribution_space": "lightgbm_raw_score_shap" },
      { "feature": "sbp_min", "feature_label": "수축기 혈압 최저",
        "text": "수축기 혈압 최저 84 mmHg", "value": 84.0,
        "contribution": 0.6621, "contribution_space": "lightgbm_raw_score_shap" }
    ],
    "reason_type": "current_risk_signal",
    "reason_title": "현재 예측에 기여한 주요 신호",
    "reason_basis": "exact_feature_current_contribution",
    "reason_notice": "모델 예측에 기여한 주요 신호이며 임상적 인과관계를 의미하지 않습니다.",
    "risk_delta": -0.0351,
    "recommendations": []
  },
  "count": 6,
  "meta": {
    "model_connected": true,
    "notice": "AI 예측은 의료진 의사결정 지원 정보이며 확정 진단이 아니다. …"
  }
}
```

**설명(reason) 계약 — riskmodel `reason_engine_v3.py` 산출 (2026-09-01 연동):**

| 필드 | 내용 |
|---|---|
| `risk_factors[]` | 화면에 그대로 쓰는 신호 문장. 최대 2개 |
| `risk_signals[]` | 같은 신호 + `feature`(모델 feature 명) · `feature_label` · 값 · 기여도 |
| `reason_type` | `risk_increase_clinical_worsening_signal` = 위험 상승 + 임상적 악화로 확인된 변화, `risk_increase_without_confirmed_clinical_worsening_signal` = 위험은 올랐지만 확인된 악화 변화 없음(**risk_factors 가 비는 것이 정상**), `current_risk_signal` = 현재 위험도 기여 |
| `clinical_worsening_confirmed` | 위험 상승 시점에서 임상적 악화로 확인된 변화가 있었는가 |
| `reason_title` | 화면 제목. 모델이 만든 문구를 그대로 쓴다 |
| `reason_basis` | `exact_feature_delta_contribution` \| `exact_feature_current_contribution` |
| `reason_notice` | 설명과 **반드시 함께** 표시해야 하는 문구 |
| `risk_delta` | 직전 예측 시점 대비 확률 변화(0~1 스케일). 첫 시점이면 `null` |
| `recommendations[]` | **항상 빈 배열.** 악화 예측 모델은 권고를 생성하지 않는다 |

🔑 **v3 변경점.** 신호 단위가 vital/lab 그룹 합산에서 **model feature 1개**로 바뀌었습니다.
   상승 신호는 그 feature 의 Δcontribution 과 **그 feature 자신의 직전/현재 값**만 씁니다
   (`previous_value` → `current_value`). 값이 실제로 변하지 않은 feature 와 정적
   feature(triage·나이·주호소·내원정보), 해석 불가능한 `cc_svd_*` 는 제외됩니다.

⚠ `contribution` · `delta_contribution` 의 단위는 `contribution_space` =
   **`lightgbm_raw_score_shap`** 입니다. **보정 확률의 %p 가 아닙니다.** 확률 변화는
   `risk_delta` 만 씁니다.

🔒 **임상 방향 gate (v3.1, 2026-09-01).** 위험 상승 신호는 fail-closed 로 걸러집니다 —
   값이 실제로 변했고, workflow proxy(측정 횟수·경과시간·mask)가 아니며, 사전 정의된
   규칙에서 `worsening` 으로 판정된 변화만 노출됩니다. 개선(SpO₂ 98→100, 승압제 1→0,
   Lactate 4→2)과 방향 미정은 제외됩니다. 각 신호에 `clinical_direction` ·
   `clinical_rule` · `clinical_gate_passed` 가 함께 옵니다.

   ⚠ **`risk_factors` 가 비어 있어도 "모델 미제공" 이 아닙니다.** `reason_type` 이
   `risk_increase_without_confirmed_clinical_worsening_signal` 이면 "확인된 악화 신호 없음"
   을 그대로 표시해야 합니다. 현재 위험 신호(`current_risk_signal`)에는 gate 가 적용되지
   않아 `hours_from_ed` 같은 비임상 proxy 가 올라올 수 있습니다.

🔴 `risk_factors` 는 LightGBM `pred_contrib` 상위 신호이며 **악화의 원인이 아니다.**
   화면 문구를 "위험요인/원인" 으로 쓰지 말고 "위험 신호" 로 쓴다.
   설명 문장은 riskmodel 이 만든다 — backend·frontend 에서 다시 만들지 않는다.

**남은 TODO:**

```text
TODO  outcome별 확률 필드 필요 여부
      respiratory_probability / vasopressor_probability /
      death_probability / cpr_probability
```

`predictions` 가 빈 배열이면(적용 범위 밖·모델 미연동) 프론트는 "AI 분석 대기 중" 빈 상태를 표시합니다.

**⏱ 예측 실행 스케줄 (2026-09-01 변경).**

예측 시점(`prediction_time`)은 **환자 입실 시각 기준 1시간 간격** 그대로입니다(10:07 입실 →
11:07 / 12:07 …). 바뀐 것은 *언제 계산하러 가는가* 뿐입니다.

```
환자별 next_prediction_at = 마지막 예측 시점 + 1h (없으면 ED 도착 + 1h)
   → due 판정: next_prediction_at ≤ 지금(원본 축)
   → 실행 슬롯: ceil15(next_prediction_at + demo_offset)   ← 데모 축, **올림**
   → slot ≤ 현재 슬롯(floor15(demo_now)) 인 환자만 batch 로 호출
```

- 슬롯은 `00 / 15 / 30 / 45` 네 개이며, 폴링(기본 60초)은 **슬롯이 바뀐 주기에만** 실행합니다.
- 올림을 쓰는 이유: riskmodel 은 `t_end`(지금)까지만 그리드를 만들어, 11:07 예측을 11:00
  슬롯에서 돌리면 그 행이 아예 생기지 않습니다(미리 계산되지 않으므로 leakage 도 없습니다).
- 서버 재시작·데모 배속으로 슬롯을 놓쳐도 선택 조건이 "slot ≤ 현재 슬롯"이라 밀린 환자가
  다음 실행에 그대로 들어옵니다(catch-up).
- `POST /api/ed/predictions/run` 은 스케줄러와 **같은 선택 로직**을 씁니다.
  `?all=true` 면 예전처럼 코호트 전원을 다시 계산합니다(데모 시계를 되돌린 뒤 복구용).

---

## 5. Frontend → API 매핑

### 연동 대상

| 화면 | 파일 | 현재 mock | 대체 API |
|---|---|---|---|
| 응급실 현황 | `routes/index.tsx` | `summary` (기록 항목 제외) | `GET /api/ed/dashboard/summary` |
| " | " | `bedZones`, `bedSummary` | `GET /api/ed/dashboard/beds` |
| " | " | `aiAlerts` | `GET /api/ed/alerts` |
| " | " | `reassessQueue` | `GET /api/ed/reassess-queue` |
| 상단 헤더 | `components/app-header.tsx` | `summary.total`, `summary.critical+rising` | `GET /api/ed/dashboard/summary` |
| 환자 목록 | `components/patient-list-table.tsx` | `sortedPatients` | `GET /api/ed/stays?sort=risk` (**`/monitoring`에서만**) |
| 환자 상세 | `routes/monitoring.$patientId.tsx` | `getPatient(id)` | `GET /api/ed/stays/{stay_id}` |
| " | " | `patient.vitals`, `patient.trend` | `GET /api/ed/stays/{stay_id}/vitals` |
| " | " | `riskFactors`, `reasonType`, `reasonNotice`, `riskDelta`, `trend[].probability` | `GET /api/ed/stays/{stay_id}/predictions` (**연동 완료**. `recommendations` 는 항상 빈 배열) |

### ⛔ 연동하지 않음 — mock 유지

| 대상 | 사유 |
|---|---|
| `/records`, `/records/$patientId` **전체** | 기록 영역 미변경 |
| `sampleDialogue`, `aiDraftRecord`, `emptyRecord`, `followUpQuestions`, `kcdCandidates` | 위와 동일 |
| `recordFieldLabels`, `checkStatusMeta`, `outcomeOptions` | 위와 동일 |
| `summary.incompleteRecords` (대시보드 카드) | 위와 동일 |
| `incompleteRecords` (대시보드 알림 목록) | 위와 동일 |
| `Patient.recordStatus` (목록 컬럼) | 위와 동일 |
| `currentUser` | 인증 미구현 |
| `riskMeta`, `bedStatusMeta` | **UI 표시 메타데이터**(색상 클래스·한글 라벨). 원래 서버 데이터가 아님 |
| 상세 화면의 "재평가 완료" / "AI 경고 확인" 버튼 | `app.alert.acknowledged_at` 연동은 후속 작업 |
| `routes/settings.tsx` | 저장되지 않는 시연 화면 |

`mock-data.ts`는 **삭제하지 않습니다.** 연동 대상 배열(`patients`, `bedZones`, `aiAlerts`, `reassessQueue`, `summary`의 일부)만 제거하고, 기록 관련 export와 UI 메타데이터는 그대로 둡니다.

---

## 6. Frontend API Client 구조

```text
frontend/src/api/
├── client.ts         # fetch 래퍼, 상대 경로, 오류 정규화
├── types.ts          # 백엔드 Pydantic 스키마에서 도출한 타입
├── ed-stays.ts       # getEdStays, getEdStay, getEdStayVitals, getEdStayPredictions
└── dashboard.ts      # getDashboardSummary, getBeds, getAlerts, getReassessQueue
```

- 이미 설치된 **TanStack Query v5**를 사용합니다(`__root.tsx`에 Provider 등록 완료, 현재 미사용).
- 컴포넌트에서 직접 `fetch`를 호출하지 않습니다.
- **`loader`가 아니라 Query로 단일화합니다.** TanStack Start의 `loader`는 SSR 시 컨테이너 안에서 실행되며, 그곳에는 `localhost:8080`(nginx)이 존재하지 않습니다. 클라이언트 페칭으로 통일하면 base URL을 하나만 관리하면 됩니다 (architecture.md §8).
- 현재 상세 라우트가 `loader`에서 mock을 동기 조회하므로, 전환 시 `loading`/`error` 상태 처리가 반드시 필요합니다.

```ts
// client.ts 개념
const BASE = import.meta.env.VITE_API_BASE_URL ?? "";   // 기본: 상대 경로

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    // nginx 502/504는 JSON이 아니라 HTML — 파싱 실패를 반드시 처리
    const detail = await res
      .json()
      .then((b) => b.detail as string)
      .catch(() => res.statusText || "서버에 연결할 수 없습니다");
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}
```

### 화면별 상태 처리 요구

| 상태 | 처리 |
|---|---|
| loading | `components/ui/skeleton.tsx` 재사용 |
| empty | 예측 미연동 시 "AI 분석 대기 중", 경고 0건 시 "경고 없음" |
| error | `ApiError.detail` 표시 + 재시도 버튼 (`__root.tsx` ErrorComponent 패턴 참고) |
| stale | `meta.is_demo_timeline` / `model_connected: false` 배지 노출 |

---

## 7. CORS

nginx 도입으로 **프론트와 API가 동일 오리진**(`localhost:8080`)이 되므로, 컨테이너 구성에서는 **CORS가 필요 없습니다.**

CORS가 필요한 경우는 하나뿐입니다: **컨테이너 밖에서 `vite dev`를 띄우고 `localhost:8100`의 backend를 직접 호출할 때.**

```python
# app/core/config.py
CORS_ORIGINS: list[str] = []   # 환경변수 CORS_ORIGINS (콤마 구분), 기본 빈 목록
```

```python
if settings.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    )
```

- 기본값은 **빈 목록**입니다. 필요할 때만 환경변수로 켭니다.
- `allow_origins=["*"]`는 사용하지 않습니다.
- 프론트 dev 포트가 `vite.config.ts`에서 고정되지 않고 Lovable 프리셋에 위임되어 있으므로(기본 5173, 샌드박스에서 변경 가능) **오리진 하드코딩을 금지**합니다.
- `/api/ed/*`가 GET 전용이므로 `allow_methods`도 `GET, OPTIONS`로 최소화합니다.

---

## 8. OpenAPI 문서

- FastAPI 기본 경로 `/docs`, `/redoc`, `/openapi.json`을 사용합니다 (**D7 확정 — 기본 유지**). `docs_url`을 바꾸지 않습니다.
- nginx가 `/docs`, `/openapi.json`을 backend로 프록시하므로 `http://localhost:8080/docs`로도 접근 가능합니다. `http://localhost:8100/docs` 직접 접근도 유지됩니다.
- 각 엔드포인트에 `summary`, `description`, `responses={404: ..., 400: ...}`를 명시합니다.
- 태그: `ED Stays`, `ED Dashboard` (기존 `Patients`, `Visits`, `Vitals`, `Predictions`, `Records`와 분리).
- **MIMIC에 없어 `null`로 반환되는 필드**(`consciousness`, `arrival_route` 등)와 **모델이 생성하지 않는 필드**(`recommendations`)는 Pydantic `Field(description=...)`에 사유를 남깁니다.

---

## 9. 확정 사항 (2026-08-26 승인)

| # | 항목 | 확정 | 참조 |
|---|---|---|---|
| D1 | 환자 이름 | `app.patient_alias` 가명 | database-design.md §5 |
| D2 | 병상 현황판 | `app.bed_assignment` 데모 배정 | database-design.md §5 |
| D4 | API 네임스페이스 | `/api/ed/*` 신설 | architecture.md §6 |
| D5 | labevents · chartevents | 코호트 서브셋 적재 (2026-08-31 개정) | database-design.md §7.7 |
| D6 | 데모 시간축 | `app.demo_stay` 도입 | database-design.md §6 |
| D7 | OpenAPI 경로 | 기본 `/docs` 유지 | 본 문서 §8 |
| D8 | 프론트 빌드 타깃 | `NITRO_PRESET=node-server` 환경변수 | architecture.md §7.4 |
| TODO | 모델 output 구조 | 최소 필드 + `detail` JSONB로 진행 | 본 문서 §4.8 |

> D3(진단코드 KCD/ICD 처리)은 기록 영역 제외에 따라 삭제되었습니다.

### 확정 후에도 남는 미확인 항목

| 항목 | 내용 |
|---|---|
| **D8 검증** | `NITRO_PRESET=node-server`가 실제로 `.output/server/index.mjs`를 만드는지 **미실측** (`node_modules` 미설치). 구현 착수 시 가장 먼저 확인하고, 실패하면 중단·보고 |
| **모델 스펙** | `risk_factors[]` 형식(문장 배열 + `risk_signals[]` 기여도) · `horizon_minutes`(180) · `t_idx`(1시간 간격) **확정 2026-09-01**. outcome별 확률 분리 여부만 미확정 |
