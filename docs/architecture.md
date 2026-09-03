# ER:ON — 시스템 아키텍처 설계

> 상태: **구현 완료**.
> 작성 2026-08-26 / 개정 2026-08-26 (rev.3 — 구현 결과 반영)
>
> ⚠ 이 문서는 **응급실 현황·모니터링 작업(rev.3)의 설계 기록**입니다. §1 현황 실측,
> §2 작업 범위, §7 compose 제안은 그 시점의 사실이므로 그대로 둡니다.
> 그 이후 배포로 달라진 부분만 아래에 덧붙입니다 (2026-09-02 기준).
>
> | 항목 | rev.3 시점 | 현재 |
> |---|---|---|
> | 악화 예측 | 모델 없음 (`PREDICT_AI_URL` 환경변수만) | `services/riskmodel` 가동 (`riskmodel:8790`, profile `risk`) |
> | 기록 초안 · STT | 범위 밖 | `clinicalnlp:8765` · `whisper:8780` 가동 (profile `clinical` / `stt`) |
> | 벡터 검색 | `QDRANT_URL` 후보 | **Qdrant 미채택.** PostgreSQL/pgvector 사용 (`docs/adr/0001-clinicalnlp-postgresql-storage.md`) |
> | 엣지 | nginx `:80` | nginx `:80` / `:443`, `eron.co.kr` HTTPS (`docs/oci-deployment.md`) |

---

## 0. 이번 개정에서 바뀐 것

| # | 변경 | 영향 문서 |
|---|---|---|
| 1 | **AI 진료기록 및 누락 검사 영역을 본 작업 범위에서 완전히 제외.** 관련 테이블·API·마이그레이션을 설계에서 삭제하고, 해당 화면은 현재 mock을 그대로 유지 | 본 문서 §2·§9, database-design.md, api-design.md |
| 2 | **frontend를 컨테이너로 추가하고 nginx를 엣지 리버스 프록시로 도입** | 본 문서 §6·§7 |
| 3 | **미결 8건 전부 확정** (D1·D2·D4·D5·D6·D7·D8·모델 TODO) | database-design.md §5 |

### 확정 사항 요약 (2026-08-26 승인)

| # | 확정 |
|---|---|
| D1 | 환자 이름 — `app.patient_alias` 결정론적 가명 |
| D2 | 병상 현황판 — `app.bed` + `app.bed_assignment` 데모 배정 |
| D4 | API 경로 — `/api/ed/*` 신설, 기존 5종 무손상 |
| D5 | labevents · chartevents — 코호트 서브셋 적재 (2026-08-31 개정) |
| D6 | 데모 시간축 — `app.demo_stay` 도입 |
| D7 | OpenAPI — FastAPI 기본 `/docs` 유지 |
| D8 | 프론트 빌드 — `NITRO_PRESET=node-server` 환경변수 (`vite.config.ts` 미변경) |
| TODO | 모델 output — 최소 필드 + `detail` JSONB |

**남은 미확인 2건**: ① D8 프리셋이 실제로 동작하는지 미실측 (§7.4) ② 모델 output 스펙 (담당자 확인 필요)

---

## 1. 현재 저장소 구조 (분석 결과)

```text
eron-project/
├── backend/                     # FastAPI + SQLAlchemy 2.0 (스캐폴딩 상태)
│   ├── app/
│   │   ├── main.py              # create_all + 5개 라우터 등록 + /health, /health/db
│   │   ├── database.py          # DATABASE_URL 환경변수, engine, SessionLocal
│   │   ├── api/                 # patients, visits, vitals, predictions, records
│   │   ├── models/              # Patient, Visit, Vital, Prediction, Record
│   │   └── schemas/             # 동일 5종 Pydantic 스키마
│   ├── Dockerfile               # python:3.12-slim, uvicorn :8000
│   └── requirements.txt         # fastapi, uvicorn, SQLAlchemy, psycopg[binary]
│
├── frontend/                    # TanStack Start + React 19 (Lovable 관리)
│   ├── src/
│   │   ├── server.ts            # SSR fetch 핸들러 (h3 오류 정규화 래퍼)
│   │   ├── start.ts             # createStart — 서버 미들웨어 + CSRF
│   │   ├── routes/              # index, monitoring.*, records.*, settings
│   │   ├── components/          # app-header, app-sidebar, patient-list-table, ui/*
│   │   └── lib/mock-data.ts     # 535줄, 전 화면이 여기에 의존
│   └── (Dockerfile 없음)
│
├── MIMIC-IV-ED/                 # 118 MB (gz)
├── MIMIC-IV-HOSP/               # 21 GB
├── MIMIC-IV-ICU/                # 43 GB
├── docker-compose.yml           # postgres(pgvector/pg16, 5433:5432) + backend(8100:8000)
└── .env.example                 # POSTGRES_*, DATABASE_URL, PREDICT_AI_URL, RECORD_AI_URL, STT_URL, QDRANT_URL
```

### 확인된 사실

| 항목 | 현황 |
|---|---|
| Frontend framework | TanStack Start 1.168 + TanStack Router 1.170 + React 19 + Vite 8 |
| **Frontend 렌더링 방식** | **SSR.** `src/server.ts`가 fetch 핸들러, `__root.tsx`가 `shellComponent`로 `<html>` 서버 렌더 |
| **Frontend 빌드 타깃** | **Cloudflare Workers (기본값).** `vite.config.ts` 주석 및 `.gitignore`의 `.wrangler/`, `.dev.vars`로 확인 |
| Frontend 상태관리 | `@tanstack/react-query` v5 **설치·Provider 등록 완료, 실사용 0건** |
| Frontend API 호출 | **전무.** `fetch` / `axios` / `import.meta.env.VITE_*` 사용처 없음 |
| Frontend 데이터 소스 | 전 화면이 `src/lib/mock-data.ts` 정적 import |
| Frontend 라우팅 | 파일 기반. `/`, `/monitoring`, `/monitoring/$patientId`, `/records`, `/records/$patientId`, `/settings` |
| Frontend 패키지 매니저 | `package-lock.json`과 `bun.lock`이 **동시 존재**. `CLAUDE.md` 기준은 **npm** |
| `node_modules` | **미설치.** 빌드 산출물 구조는 실측하지 못함 |
| Backend | 존재. 범용 CRUD 5종. **MIMIC과 무관한 자체 도메인 모델** |
| Backend 마이그레이션 | 없음 (`Base.metadata.create_all`) |
| Backend CORS | **미설정** |
| Backend 스키마 분리 | 없음 (전부 `public`) |
| Docker | postgres + backend 2개 서비스. **frontend·nginx 서비스 없음** |
| 모델 코드 / inference | **저장소 내 존재하지 않음** (`PREDICT_AI_URL` 등 환경변수만 정의) |

---

## 2. 작업 범위

### 이번 작업에 포함

```text
응급실 현황 (/)            ─ 요약 카드 · 병상 현황판 · AI 경고 · 재평가 우선순위
환자 모니터링 (/monitoring) ─ 환자 목록
환자 상세 (/monitoring/$patientId) ─ 헤더 · Vital · 시간별 추이 · AI 분석
```

### 이번 작업에서 **제외** (기록 영역 — 손대지 않음)

```text
AI 진료기록 및 누락 검사 (/records, /records/$patientId)
 ├─ 5단계 워크플로우 (대화 수집 → 기록 초안 → 누락 검사 → 진단코드 → 의사 인증)
 ├─ sampleDialogue / aiDraftRecord / followUpQuestions / kcdCandidates
 ├─ recordFieldLabels / checkStatusMeta / outcomeOptions / emptyRecord
 └─ 대시보드의 "기록 미완료 알림" 및 "기록 미완료" 요약 카드
```

**해당 화면·데이터·컴포넌트는 현재 mock 상태 그대로 유지합니다.**
DB 테이블(`app.ed_record`), API(`/api/ed/records/*`, `/api/ed/stays/{id}/record`, `/api/ed/stays/{id}/diagnoses`), 프론트 연동을 **설계에서 삭제**했습니다.
`RECORD_AI_URL` / `STT_URL` 연동도 범위 밖입니다.

> 부수 효과: 환자 목록의 `기록 상태` 컬럼과 대시보드의 `기록 미완료` 항목은 API가 아니라 **mock 값을 계속 사용**합니다. `CLAUDE.md`의 "mock과 live를 한 흐름에서 섞지 않는다" 규칙에 대응해, 해당 요소에는 UI 변경 없이 응답 `meta`로 출처를 구분합니다 (R6: 디자인 불변).

---

## 3. 목표 아키텍처

```text
                         ┌──────────────┐
                         │   Browser    │
                         └──────┬───────┘
                                │ http://localhost:8080
                                ▼
        ┌───────────────────────────────────────────────┐
        │  nginx  (edge / reverse proxy)  :80 / :443    │
        │                                               │
        │   location /api/   ──►  backend:8000          │
        │   location /       ──►  frontend:3000         │
        └────────────┬──────────────────────┬───────────┘
                     │                      │
        ┌────────────▼───────────┐  ┌───────▼─────────────────────┐
        │  frontend              │  │  backend — FastAPI          │
        │  TanStack Start SSR    │  │                             │
        │  node .output/server   │  │   api/         라우터·검증   │
        │  :3000                 │  │     ▼                       │
        │                        │  │   services/    화면 조합     │
        │  routes/*              │  │     ▼          위험도·단위   │
        │    ▼                   │  │   repositories/ 조회 전용    │
        │  src/api/*  (신설)     │  │     ▼                       │
        │    ▼                   │  │   models/ · schemas/        │
        │  TanStack Query        │  │                    :8000    │
        └────────────────────────┘  └───────────┬─────────────────┘
                                                │ SQLAlchemy 2.0 (psycopg3)
                                                ▼
                          ┌─────────────────────────────────────┐
                          │  PostgreSQL 16 (pgvector)   :5432   │
                          │                                     │
                          │   schema mimic  ← MIMIC 서브셋(RO)  │
                          │   schema app    ← 예측·데모 (RW)    │
                          │   schema public ← 기존 CRUD (현행)  │
                          └─────────────────┬───────────────────┘
                                            ▲
                                            │ 오프라인 1회 적재 (COPY, 행 서브셋)
                                            │
                          MIMIC-IV-ED / HOSP / ICU  CSV(.gz)
```

### AI 서비스 — rev.3 당시 "향후", 현재는 모두 가동 중

rev.3 에서는 셋 다 본 작업 범위 밖이었습니다. 이후 각각 마이크로서비스로 추가되어
지금은 profile 로 켜고 끕니다. 전부 Docker 내부 DNS 로만 호출하며 호스트·외부에
포트를 공개하지 않습니다.

```text
Backend ──HTTP──> riskmodel:8790    (악화 예측)   → app.prediction 적재   profile: risk
        ──HTTP──> clinicalnlp:8765  (기록 초안)                          profile: clinical
        ──HTTP──> whisper:8780      (음성 인식)                          profile: stt
```

profile 이 꺼져 있어도 backend 자체는 기동하며, 해당 endpoint 만 503 을 반환합니다.

> **`QDRANT_URL` 은 채택되지 않았습니다.** 의료용어·정책 벡터 검색은 별도 VectorDB
> 없이 PostgreSQL/pgvector 로 처리합니다 — `docs/adr/0001-clinicalnlp-postgresql-storage.md`.
> `.env.example` 의 `QDRANT_URL` 자리는 사용되지 않는 잔재입니다.

---

## 4. 레이어 책임 분리

| 레이어 | 책임 | 하지 않는 것 |
|---|---|---|
| `nginx` | 단일 진입점, 경로 라우팅, gzip, 타임아웃 | 인증, 비즈니스 로직 |
| `api/` | 라우팅, 쿼리 파라미터 검증, HTTP 상태코드, OpenAPI 문서 | 비즈니스 로직, SQL |
| `services/` | 화면 단위 응답 조합, 위험도 등급 산출, °F→°C 변환, 데모 시간축 변환 | 직접 SQL |
| `repositories/` | 필요한 컬럼만 SELECT, 페이지네이션, 인덱스 친화적 쿼리 | HTTP 개념, 등급 판정 |
| `models/` | SQLAlchemy 테이블 정의 (`mimic` / `app` 스키마 분리) | — |
| `schemas/` | Pydantic 요청·응답 계약 = **프론트엔드 타입의 원천** | — |

---

## 5. 스키마 3분할 근거

| 스키마 | 내용 | 쓰기 | 이유 |
|---|---|---|---|
| `mimic` | edstays, triage, ed_vitalsign, ed_diagnosis, patients, admissions, icustays, labevents, chartevents | ❌ 적재 스크립트만 | 원천 데이터 불변성 보장. 재적재 시 스키마 단위 DROP 가능 |
| `app` | prediction, prediction_ack, bed, bed_assignment, patient_alias, demo_stay, alert, cohort, demo_clock | ✅ | 애플리케이션이 생성하는 데이터. Alembic 마이그레이션 대상 |
| `public` | patients, visits, vitals, predictions, records (기존) + clinical_records, kcd_codes | ✅ | 기존 CRUD API 계약 유지 (`CLAUDE.md` 규칙) |

rev.3 이후 추가된 테이블입니다.

| 테이블 | 추가 배경 | 생성 경로 |
|---|---|---|
| `mimic.labevents` · `mimic.chartevents` | riskmodel 의 lab feature 36개(전체 100개 중)와 ICU 활력징후 보조 원천 | `database/init/01_schema.sql` |
| `app.prediction_ack` | 의료진 "재검토 완료" 확인 상태. 경고 자체는 `app.prediction` 에서 조회 시점에 파생한다 | `database/init/01_schema.sql` |
| `public.clinical_records` · `public.kcd_codes` | 응급진료기록 임시·인증 저장과 KCD 코드 조회 | backend 기동 시 `Base.metadata.create_all` |

> `public` 의 두 테이블은 init SQL 이 아니라 SQLAlchemy 가 만듭니다. 볼륨이 비어 있을
> 때만 도는 `docker-entrypoint-initdb.d` 와 생성 시점이 다르므로, 스키마를 손으로
> 맞출 때 빠뜨리기 쉽습니다.

**핵심 원칙**: MIMIC 원천 적재는 Alembic 마이그레이션에 넣지 않습니다. `mimic` 스키마는 `database/init/` SQL + 적재 스크립트로 관리하고, Alembic은 `app` 스키마만 추적합니다.

---

## 6. 기존 Backend와의 충돌 — **D4 확정**

기존 `/api/patients`는 **자체 도메인 Patient(자동증가 id, patient_number, name)** 를 반환합니다.
요청서 §13의 `GET /api/patients/{stay_id}`는 **MIMIC ED stay**를 의미합니다. 같은 경로, 다른 자원입니다.

| 안 | 내용 | 영향 |
|---|---|---|
| **A (권장)** | `/api/ed/*` 신규 네임스페이스 신설, 기존 `/api/patients` 유지 | 기존 계약 무손상. 두 도메인이 실제로 다르므로 의미상 정합 |
| B | `/api/patients`를 MIMIC ED stay로 교체 | 기존 5개 라우터 계약 파기. `CLAUDE.md` "기존 route prefix·응답 형태 유지" 규칙 위반 |
| C | `/api/v2/patients` 버저닝 | 두 버전 동시 유지 부담 |

→ **A안 확정 (2026-08-26 승인).** 프론트엔드가 현재 백엔드를 전혀 호출하지 않으므로 신규 네임스페이스 도입 비용이 0입니다.

---

## 7. 컨테이너 구성 (개정)

### 7.1 서비스 4종

| 서비스 | 이미지 / 빌드 | 컨테이너 포트 | 호스트 공개 | 역할 |
|---|---|---|---|---|
| `nginx` | `nginx:1.27-alpine` + 설정 마운트 | 80 | **8080** | 유일한 진입점. `/api`→backend, `/`→frontend |
| `frontend` | `./frontend/Dockerfile` (신규) | 3000 | ❌ 비공개 | TanStack Start SSR Node 서버 |
| `backend` | `./backend/Dockerfile` (기존) | 8000 | **8100** (유지) | FastAPI. Swagger·헬스체크 직접 접근용 |
| `postgres` | `pgvector/pgvector:pg16` (기존) | 5432 | **5433** (유지) | DB |

- backend `8100`, postgres `5433` 공개는 **기존 `docker-compose.yml`과 `CLAUDE.md` 검증 절차**(`http://localhost:8100/health`)를 깨지 않기 위해 유지합니다.
- frontend는 nginx를 통해서만 접근합니다.

### 7.2 nginx 설정 (초안)

```nginx
# nginx/conf.d/eron.conf
upstream eron_backend  { server backend:8000; }
upstream eron_frontend { server frontend:3000; }

server {
    listen 80;
    server_name _;

    gzip on;
    gzip_types text/plain text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1024;

    # 백엔드 API — 프론트보다 먼저 매칭되어야 함
    location /api/ {
        proxy_pass         http://eron_backend;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }

    # Swagger (개발 편의)
    location /docs      { proxy_pass http://eron_backend; }
    location /openapi.json { proxy_pass http://eron_backend; }

    # 프론트엔드 SSR — 나머지 전부
    location / {
        proxy_pass         http://eron_frontend;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_set_header   Upgrade           $http_upgrade;   # dev HMR
        proxy_set_header   Connection        "upgrade";
    }
}
```

> `location /api/`가 `location /`보다 **더 긴 접두사**이므로 nginx 매칭 규칙상 우선합니다. 순서에 의존하지 않습니다.

### 7.3 frontend Dockerfile (초안)

```dockerfile
# frontend/Dockerfile
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build          # → .output/  (nitro node-server preset 전제)

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production PORT=3000
COPY --from=build /app/.output ./.output
EXPOSE 3000
CMD ["node", ".output/server/index.mjs"]
```

- `npm ci` 사용 (`CLAUDE.md`의 npm 표준 준수). `bun.lock`은 사용하지 않습니다.
- `.dockerignore`에 `node_modules`, `.output`, `.wrangler`를 추가합니다.

### 7.4 ⚠️ 블로커 — 빌드 타깃이 Cloudflare Workers

`vite.config.ts`는 `@lovable.dev/vite-tanstack-config` 프리셋에 빌드를 위임하며, 주석에 **"nitro (build-only using cloudflare as a default target)"** 라고 명시되어 있습니다. `frontend/.gitignore`의 `.wrangler/`·`.dev.vars`도 이를 뒷받침합니다.

즉 **현재 설정으로 `npm run build`를 하면 Cloudflare Worker 번들이 나오며, `.output/server/index.mjs`(Node 서버)가 생성되지 않습니다.** nginx가 정적 서빙할 수 있는 SPA 산출물도 아닙니다.

**D8 확정 (2026-08-26 승인): `NITRO_PRESET=node-server` 환경변수 방식.**

```dockerfile
# frontend/Dockerfile — build 스테이지
ENV NITRO_PRESET=node-server
RUN npm run build
```

`vite.config.ts`는 **수정하지 않습니다.** Lovable 관리 파일이며 프리셋 주석에 "수동 추가 금지" 경고가 있어, 에디터가 덮어쓸 위험을 피합니다. 로컬 `vite dev` 동작도 그대로 유지됩니다.

| 검토했으나 채택하지 않은 안 | 사유 |
|---|---|
| `vite.config.ts`에 `nitro: { preset: "node-server" }` 직접 추가 | Lovable 에디터가 덮어쓸 수 있음 |
| SPA로 전환 (SSR 포기) | `server.ts`·`start.ts`·`shellComponent` 구조를 깨야 함. Lovable 워크플로우 훼손 |
| frontend 컨테이너화 포기 | 요청과 불일치 |

> ✅ **검증 완료 (2026-08-26)**: `NITRO_PRESET=node-server npm run build` 로
> `.output/server/index.mjs` 가 생성되고 `nitro.json` 의 `preset` 이 `node-server` 로 찍혔습니다.
> `node .output/server/index.mjs` 로 기동해 HTTP 200 을 확인했습니다.
>
> ⚠️ **부수적으로 발견한 문제**: 커밋된 `frontend/package-lock.json` 이 `package.json` 과
> 어긋나 `npm ci` 가 실패했습니다 (`json-schema-traverse@0.4.1` 누락). Docker 빌드가
> `npm ci` 를 쓰므로 `npm install` 로 lockfile 을 동기화했습니다 (+95 / −16 줄).
> `package.json` 은 변경하지 않았습니다.

### 7.5 docker-compose 구조 (초안)

```yaml
services:
  postgres:  # 기존 그대로 (5433:5432)
  backend:   # 기존 그대로 (8100:8000) + CORS_ORIGINS 추가
  frontend:
    build: { context: ./frontend }
    expose: ["3000"]
    depends_on: [backend]
    networks: [eron-network]
  nginx:
    image: nginx:1.27-alpine
    ports: ["8080:80"]
    volumes: ["./nginx/conf.d:/etc/nginx/conf.d:ro"]
    depends_on: [frontend, backend]
    networks: [eron-network]
```

기존 `eron-network` 브리지와 `eron_postgres_data` 볼륨을 그대로 사용합니다.

---

## 8. 포트 · 환경변수

### 포트

| 구성요소 | 호스트 | 컨테이너 | 비고 |
|---|---|---|---|
| **nginx** | **8080** | 80 | **주 진입점** |
| frontend | — | 3000 | 비공개 |
| backend | 8100 | 8000 | 기존 유지 (Swagger·헬스체크) |
| PostgreSQL | 5433 | 5432 | 기존 유지 |
| frontend (로컬 dev, 컨테이너 밖) | 미고정 | — | Lovable 프리셋 위임. 기본 5173 |

### 환경변수

| 변수 | 위치 | 예시 | 비고 |
|---|---|---|---|
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | compose | — | 기존 |
| `DATABASE_URL` | backend | `postgresql+psycopg://user:pw@postgres:5432/eron` | 기존 |
| `CORS_ORIGINS` | backend (**신규**) | `http://localhost:5173` | 아래 참조 |
| `MIMIC_DATA_DIR` | 적재 스크립트 (**신규**) | 저장소 루트 | |
| `VITE_API_BASE_URL` | frontend (**신규**) | **빈 값 권장** | 아래 참조 |
| `NITRO_PRESET` | frontend 빌드 (**신규**) | `node-server` | §7.4 |

#### `VITE_API_BASE_URL`을 비워 두기를 권장하는 이유

1. nginx가 프론트와 API를 **동일 오리진**(`localhost:8080`)으로 노출하므로, 브라우저는 `/api/...` 상대 경로로 호출하면 됩니다.
2. **CORS가 프로덕션에서 불필요해집니다.** 로컬 `vite dev`(컨테이너 밖)에서만 `CORS_ORIGINS`가 필요합니다.
3. `VITE_*`는 **빌드 시점에 번들에 고정**되므로 런타임 주입이 불가능합니다. 절대 URL을 넣으면 이미지 재빌드 없이는 바꿀 수 없습니다. 상대 경로면 이 문제가 사라집니다.

> **SSR 주의**: TanStack Start의 `loader`는 서버에서도 실행되며, 서버 컨테이너에는 `localhost:8080`이 존재하지 않습니다. 따라서 데이터 조회는 **TanStack Query 기반 클라이언트 페칭으로 단일화**하고 `loader`에서 API를 호출하지 않습니다 (api-design.md §6). 이 원칙을 어기면 SSR 전용 base URL(`http://backend:8000`)을 따로 관리해야 합니다.

`.env.example`에 신규 항목을 추가하되 **값은 비워 둡니다.**

---

## 9. 데이터 흐름 예시

### 환자 목록

```text
Browser /monitoring
   │ GET /api/ed/stays?page=1&page_size=20        (상대 경로, 동일 오리진)
   ▼
nginx  location /api/  ──►  backend:8000
   ▼
service:
   mimic.edstays ⋈ mimic.triage ⋈ mimic.patients
                 ⋈ app.v_latest_vitalsign   (stay당 최신 1행, LATERAL)
                 ⋈ app.v_latest_prediction  (stay당 최신 1행, LATERAL)
                 ⋈ app.patient_alias · app.bed_assignment
   ▼
   위험도 등급 산출(80/60/30) · °F→°C · 데모 시간축 변환
   ▼ JSON
PatientListTable 렌더
   └─ "기록 상태" 컬럼만 mock 유지 (기록 영역 범위 밖)
```

### 환자 상세 + 추이

```text
/monitoring/$patientId
   GET /api/ed/stays/{stay_id}
   GET /api/ed/stays/{stay_id}/vitals
   GET /api/ed/stays/{stay_id}/predictions
```

`stay_id` 하나로 3개 테이블을 각각 인덱스 조회합니다. 전체 스캔·`SELECT *`는 발생하지 않습니다.

---

## 10. 최종 디렉터리 (제안)

```text
eron-project/
├── nginx/
│   └── conf.d/eron.conf         # 신규
│
├── backend/app/
│   ├── api/
│   │   ├── ed_stays.py          # 신규
│   │   ├── ed_dashboard.py      # 신규
│   │   ├── ed_predictions.py    # 신규
│   │   └── patients.py … records.py   # 기존 유지 (변경 없음)
│   ├── core/config.py           # 신규 (환경변수 집약)
│   ├── models/{mimic,app}/      # 신규 (스키마별 분리)
│   ├── repositories/            # 신규
│   ├── services/                # 신규
│   └── schemas/ed/              # 신규
├── backend/alembic/             # app 스키마만 추적
│
├── frontend/
│   ├── Dockerfile               # 신규
│   ├── .dockerignore            # 신규
│   └── src/api/                 # 신규 (client, types, ed-stays, dashboard)
│
├── database/
│   ├── init/01_schema.sql       # mimic/app 스키마 + 테이블 + 인덱스
│   └── scripts/
│       ├── _db.py               # psql 기반 공용 DB 헬퍼
│       ├── select_cohort.py     # 코호트 선별 → app.cohort 테이블
│       ├── load_subset.py       # 서브셋 COPY 적재 (기본)
│       └── load_full.py         # 전체 적재 (승인 시에만 실행)
│
├── docker-compose.yml           # nginx · frontend 서비스 추가
├── .env.example                 # 신규 변수 추가
└── docs/
    ├── architecture.md  database-design.md  api-design.md
```

**변경하지 않는 것**: `frontend/src/routes/records.*`, `frontend/src/lib/mock-data.ts`의 기록 관련 export, `backend/app/api/records.py`, `backend/app/models/record.py`, `backend/app/schemas/record.py`.
