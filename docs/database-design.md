# ER:ON — 데이터베이스 설계

> 상태: **구현 완료**. 실제 적재 결과를 반영합니다.
> 작성 2026-08-26 / 개정 2026-08-26 (rev.3 — 구현 결과 반영)
> 2026-09-01 개정 — riskmodel 배포에 맞춰 §7.7 적재 전략과 §9 DDL 을 갱신
> 2026-09-02 개정 — §8 ERD 를 로컬 DB 실측으로 재작성, §12.1 프로젝트 테이블 이관 절차 추가
> 2026-09-04 개정 — 대시보드 기록 미완료 알림을 `public.clinical_records` 기반 live 조회로 전환
> DBMS: PostgreSQL 16 (pgvector 이미지)

---

## 0. 이번 개정에서 바뀐 것

**AI 진료기록 및 누락 검사 영역을 본 작업 범위에서 완전히 제외했습니다.**

| 삭제된 설계 요소 | 사유 |
|---|---|
| `app.ed_record` 테이블 | 기록 영역 미변경 |
| 기록 관련 인덱스 (구 #14) | 위와 동일 |
| 결정항목 D3 (KCD/ICD 처리) | 진단코드 추천은 기록 화면 기능 |
| `ed/medrecon`, `ed/pyxis`의 Phase B 후보 지위 | 복용약 항목은 기록 화면 전용 → **완전 제외** |

이 절은 초기 데이터베이스 설계 작업 당시의 제외 범위를 기록합니다. 현재 응급진료기록은 `public.clinical_records`에 저장되며, 대시보드의 "기록 미완료" 카드·알림은 해당 저장 데이터를 조회합니다.

**추가로 §7 (행 서브셋 전략)을 실행 가능한 수준으로 구체화했습니다.**

| 보강 내용 | 위치 |
|---|---|
| 계층별 적격 풀 **전수 스캔 실측치** (A 17,135 / B 47,670 / C 40,269 / D **48**) | §7.2 |
| acuity 쿼터 확정 — 코호트 전체에서 acuity 1~5 전부 등장 | §7.3 |
| 선별 알고리즘 8단계 + 결정론적 정렬키(SEED 고정) | §7.4 |
| 적재 후 자동 검증 SQL 6종 | §7.6 |

---

## 0-1. 구현 결과

**2026-08-28 현재 DB 는 MIMIC-IV Clinical Database Demo v2.2 로 적재되어 있습니다.**
정식 데이터 구성은 비교용으로 남겨둡니다. 스키마·뷰·API 는 두 구성에서 동일합니다.

| 테이블 | 데모 (현재) | 정식 (2026-08-26) |
|---|---:|---:|
| `mimic.patients` | 34 | 300 |
| `mimic.admissions` | 77 | 249 |
| `mimic.edstays` | **83** | 300 |
| `mimic.triage` | 83 | 300 |
| `mimic.ed_vitalsign` | **665** | 2,536 |
| `mimic.ed_diagnosis` | 191 | 705 |
| `mimic.icustays` | 34 | 107 |
| `app.patient_alias` / `app.demo_stay` | 83 / 83 | 300 / 300 |
| `app.bed` / `app.bed_assignment` | 84 / 83 | 84 / 83 |
| `app.prediction` / `app.alert` | **0 / 0** (모델 미연동) | 0 / 0 |
| **합계** | **1,397 행** | 5,161 행 |

검증 13항목 전부 통과 (§7.6).

### 데모 데이터셋 전환 (2026-08-28)

| 항목 | 정식 | 데모 |
|---|---:|---:|
| ED stay | 425,087 | **222** |
| ED 방문 보유 환자 | 205,504 | **64** |
| `labevents` | 158,374,764 | **107,727** |
| `chartevents` | 432,997,491 | **668,862** |
| 위 두 파일 전량 스캔 | 약 6분 (실측) | **0.7초 (실측)** |

파일 10종의 CSV 헤더가 정식본과 **완전히 일치**하고 참조 무결성도 깨끗해
(`edstays→patients` 고아 0, `edstays→admissions` 고아 0), 적재 코드 변경 없이
`MIMIC_DATA_DIR` 지정만으로 전환됩니다.

**데모 데이터의 한계 — 없는 값을 만들지 않는다 (R4)**

| 한계 | 실측 | 영향 |
|---|---|---|
| ED 사망(`EXPIRED`) 0건 | disposition: ADMITTED 150 · HOME 60 · TRANSFER 5 · 기타 7 | **계층 D 소멸.** 악화 라벨은 계층 A(ICU 이동)만으로 구성 |
| acuity 5 = 0건, acuity 4 = 2건 | 1=18 · 2=97 · 3=90 · 4=2 · 5=0 · NULL=15 | KTAS 4·5 배지 미렌더. 그 2건도 vital 5회 미만이라 탈락 |
| 재방문 집중 | 64명이 222 stay (최다 23회 방문) | 코호트 83건의 고유 환자는 34명. API 는 `stay_id` 만 노출하므로 화면상 중복으로 보이지 않음 |

코호트는 쿼터 없이 **적격 전량(83건)** 을 선별합니다 (`select_cohort.py --all`).
적격 풀이 작아 쿼터로 골라낼 실익이 없기 때문입니다. `--all` 을 정식 데이터에
쓰면 20만 건 이상이 잡히므로 **2,000건 상한**에서 중단됩니다.

### 구현하며 설계에서 바뀐 것

| 항목 | 설계안 | 구현 | 사유 |
|---|---|---|---|
| FK 제약 | 테이블 DDL 에 인라인 | **적재 후 `03_constraints.sql`** | COPY 순서 의존 제거. 고아 행이 있으면 제약 부여 시점에 실패하므로 무결성은 동일하게 보장 |
| 데모 시간축 기준점 | 내원 시각(`intime`) | **마지막 vital 측정 시각** | 내원 기준으로 잡으면 체류가 긴 환자의 vital 이 미래 시각이 됨. 실제로 1,680행이 미래로 나왔고 기준점을 바꿔 해소 |
| 데모 앵커 시계 | 호스트 `datetime.now()` | **DB `now()`** + 컨테이너 `TZ` 정렬 | 호스트(KST)와 컨테이너(UTC) 시계가 9시간 어긋나 `now()` 비교가 전부 깨짐 |
| 적재 도구 | psycopg | **컨테이너 안 psql `\copy`** | 로컬에 psycopg/psql 설치를 요구하지 않기 위함. COPY 사용은 동일 |
| `°F→℃` 변환 | service 레이어 | **`mimic.v_ed_vitalsign_clean` view** | 이상치 필터와 같은 곳에서 한 번만 처리 |

---

## 1. 프론트엔드 화면별 데이터 요구사항

`frontend/src/lib/mock-data.ts`(535줄)와 4개 화면 코드를 전수 분석한 결과입니다.

### 1.1 응급실 현황 (`/`, routes/index.tsx)

```text
Dashboard
├─ 요약 카드 5종        : total / critical / rising / incompleteRecords / aiAlertsToday
├─ 병상 현황판          : 14구역 × 6병상 = 84 (화면은 48 + 36 두 페이지)
│   └─ 병상별           : bed_id, status(critical|moderate|low|empty), 이름, 나이, 성별,
│                         장비 배지(E=ECMO, V=인공호흡기, C=CRRT), patientId 링크
├─ 실시간 AI 경고       : time, patient, patientId, level, message
├─ 위험 환자 우선순위   : patient, patientId, due("즉시"/"10분 내"), risk
└─ 기록 미완료 알림     : patient, patientId, missing(누락 항목 문자열)
```

### 1.2 환자 모니터링 목록 (`/monitoring`, components/patient-list-table.tsx)

```text
컬럼 11종
환자번호 / 이름 / 성별·나이 / 내원시간 / KTAS / 주증상 /
현재 위험도 / 악화 예측 확률 / 최근 Vital(HR·BP·SpO₂) / 기록 상태 / 상세보기
정렬: riskOrder(critical>rising>watch>stable) → deteriorationProbability desc
```

### 1.3 환자 상세 (`/monitoring/$patientId`)

```text
헤더      : id, name, sex, age, arrivedAt, arrivalRoute, arrivalMeans,
            ktas, chiefComplaintDetail, risk, deteriorationProbability
현재 Vital: hr, rr, sbp/dbp, bt(℃), spo2, mental
            ※ 이상치 판정 임계값이 컴포넌트에 하드코딩:
              hr>100|<50, rr>20|<10, sbp<100|>160, bt>=37.5, spo2<94
시간별 변화: trend[] = { time, hr, sbp, dbp, spo2, bt, probability }  (LineChart + 표)
AI 분석    : risk 등급, riskFactors[], recommendations[]
액션       : 의료진 재평가 완료 / AI 경고 확인 (현재 로컬 state)
```

### 1.4 AI 진료기록 (`/records/$patientId`) — ⛔ **본 작업 범위 밖**

> 아래는 분석 기록용입니다. **이번 작업에서 DB·API·프론트 어느 것도 만들지 않습니다.**
> 해당 화면은 현재 mock 상태를 그대로 유지합니다.

```text
5단계 워크플로우
1 대화 수집  : sampleDialogue[]  (STT 결과)
2 기록 초안  : aiDraftRecord — 11개 필드
               chiefComplaint, painAssessment, presentIllness, pastHistory,
               medication, allergy, socialHistory, systemReview,
               physicalExam, impression, outcome
3 누락 검사  : 필드별 complete|review|missing, 완전성 %
4 진단코드   : kcdCandidates[] = { rank, name, code, fitness, reasons[] }
5 의사 인증  : certifiedAt, 서명 정보
```

---

## 2. MIMIC-IV 데이터 실측 (분석 결과)

### 2.1 파일 목록 · 크기 · 행 수

| 데이터셋 | 파일 | 압축 크기 | 행 수 | 사용 |
|---|---|---:|---:|:--:|
| ED | `edstays.csv.gz` | 9.8 MB | 425,087 | ✅ |
| ED | `triage.csv.gz` | 9.5 MB | 425,087 | ✅ |
| ED | `vitalsign.csv.gz` | 24 MB | 1,564,610 | ✅ |
| ED | `diagnosis.csv.gz` | 12 MB | 899,050 | ✅ (적재만, API 미노출) |
| ED | `medrecon.csv.gz` | 45 MB | 2,987,342 | ❌ |
| ED | `pyxis.csv.gz` | 16 MB | 1,586,053 | ❌ |
| HOSP | `patients.csv.gz` | 2.7 MB | 364,627 | ✅ |
| HOSP | `admissions.csv.gz` | 19 MB | 546,028 | ✅ |
| HOSP | `transfers.csv.gz` | 44 MB | 2,413,581 | ❌ |
| HOSP | `diagnoses_icd.csv.gz` | 32 MB | 6,364,488 | ❌ |
| HOSP | `d_labitems.csv.gz` | 13 KB | 1,650 | ⚠️ Phase B |
| HOSP | `labevents.csv.gz` | 2.4 GB | **158,374,764** (원본 17.1 GB) | ✅ (코호트 서브셋) |
| HOSP | `omr.csv.gz` | 42 MB | — | ❌ |
| HOSP | `prescriptions.csv.gz` | 579 MB | — | ❌ |
| HOSP | `services.csv.gz` | 8.2 MB | — | ❌ |
| HOSP | `emar.csv` / `emar_detail.csv` | 5.8 GB / 8.1 GB (비압축) | — | ❌ |
| ICU | `icustays.csv.gz` | 3.2 MB | 94,458 | ✅ |
| ICU | `d_items.csv.gz` | 57 KB | 4,095 | ⚠️ Phase B |
| ICU | `chartevents.csv.gz` | 3.3 GB | **≈433,000,000** (원본 39 GB) | ✅ (코호트 서브셋 · itemid whitelist) |
| ICU | `inputevents.csv.gz` | 383 MB | — | ⚠️ Phase B |
| ICU | `procedureevents.csv.gz` | 23 MB | — | ⚠️ Phase B |
| ICU | `outputevents.csv.gz` | 47 MB | — | ❌ |

**총 원본 용량 ≈ 64 GB.** 로컬 가용 디스크 1.2 TB — 저장 공간 자체는 문제가 아니나, 전체 적재 시 PostgreSQL 인덱스 포함 **150 GB+** 및 수 시간~수십 시간의 적재 시간이 발생합니다. → 서브셋 적재가 필수입니다.

### 2.2 연결 관계 실측

```text
edstays 총 425,087건
 ├─ hadm_id 있음 (입원 연결)      203,016  (47.8%)
 ├─ disposition = ADMITTED        158,010  (37.2%)
 └─ ICU stay까지 연결됨            31,916   (7.5%)  ← 악화(deterioration) 양성 풀

disposition 분포
  HOME 241,632 / ADMITTED 158,010 / TRANSFER 7,025 /
  LEFT WITHOUT BEING SEEN 6,155 / ELOPED 5,710 / OTHER 4,297 /
  LEFT AGAINST MEDICAL ADVICE 1,881 / EXPIRED 377

triage.acuity 분포 (ESI)
  3: 225,060 / 2: 139,407 / 4: 28,504 / 1: 24,018 / 5: 1,100 / NULL: 6,987

edstays.arrival_transport 분포
  WALK IN 251,849 / AMBULANCE 155,752 / UNKNOWN 15,352 / OTHER 1,266 / HELICOPTER 868

vitalsign stay당 측정 횟수
  측정 있는 stay 408,146 / 평균 3.8회
  ≥3회 233,332 · ≥5회 111,073 · ≥8회 42,620 · ≥12회 13,873
```

### 2.3 데이터 품질 이슈 (실측)

| 이슈 | 근거 | 대응 |
|---|---|---|
| **체온이 °F** | `triage.temperature` 값이 97.8 / 98.4 | `mimic` 스키마엔 원본 °F 저장, service 레이어에서 °C 변환 |
| **`pain`이 자유텍스트** | 425,087행 중 **15,218행 비수치** — `unable`, `uta`, `Critical`, `ua`, `6-7`, `c` 등 | `pain` 컬럼 **TEXT**로 정의. 숫자 캐스팅 금지 |
| **생리학적 불가능 값** | `sbp` max 151,103 · `dbp` max 661,672 · `temperature` max 986 · `o2sat` max 9,322 · `heartrate` max 1,228 | 적재는 원본 그대로. **조회 시 view에서 plausibility 필터** 적용 |
| **`chiefcomplaint`에 콤마 포함** | `"Abd pain, Abdominal distention"` | `COPY … FORMAT csv` 필수. 라인 스플릿 파싱 금지 |
| **`rhythm` 96.19% NULL** | 1,564,610행 중 1,504,950 NULL | 의식수준(mental) 대체 불가 |
| **날짜가 환자별로 shift됨** | intime 연도 2110~2210+ 분포 | "현재 재실 환자" 개념 성립 불가 → §6 데모 시간축 참조 |
| **NULL 비율** | triage: temp 5.51% / hr 4.02% / rr 4.79% / o2sat 4.85% / sbp 4.30% / dbp 4.49% / acuity 1.64%<br>vitalsign: temp 36.11% / hr 4.46% / rr 5.71% / o2sat 8.68% / sbp·dbp 5.19% | 전 vital 컬럼 **NULL 허용**. 프론트 빈 상태 처리 필요 |

---

## 3. Frontend 필드 ↔ MIMIC 매핑 (전수)

### ✅ MIMIC에서 직접 조달 가능

| Frontend 필드 | 출처 | 변환 |
|---|---|---|
| `id` | `mimic.edstays.stay_id` | bigint → string |
| `sex` | `mimic.edstays.gender` | `M`→"남", `F`→"여" |
| `age` | `mimic.patients.anchor_age + (year(intime) - anchor_year)` | 계산 |
| `arrivedAt` | `mimic.edstays.intime` | 데모 시간축 변환 |
| `arrivalMeans` | `mimic.edstays.arrival_transport` | WALK IN→"도보", AMBULANCE→"119 구급차" 등 |
| `ktas` | `mimic.triage.acuity` | ⚠️ **ESI이지 KTAS 아님** (§4 참조) |
| `chiefComplaint` | `mimic.triage.chiefcomplaint` | 첫 항목 |
| `chiefComplaintDetail` | `mimic.triage.chiefcomplaint` | 전체 문자열 |
| `vitals.hr/rr/sbp/dbp/spo2` | `mimic.ed_vitalsign` 최신행 | plausibility 필터 |
| `vitals.bt` | `mimic.ed_vitalsign.temperature` | **°F → °C** |
| `trend[].{time,hr,sbp,dbp,spo2,bt}` | `mimic.ed_vitalsign` ORDER BY charttime | 동일 |

### ⚠️ 부분 조달

| Frontend 필드 | 상황 |
|---|---|
| `arrivalRoute` ("직접 내원"/"타병원 전원") | `admissions.admission_location`(`TRANSFER FROM HOSPITAL`, `EMERGENCY ROOM` 등)로 근사. **hadm_id가 있는 47.8%만** 판정 가능. 나머지는 "미상" |

### ❌ MIMIC에 없음 — 앱 계층 필요 (R4: 지어내지 않음)

| Frontend 필드 | 사유 | 처리안 |
|---|---|---|
| `name` (김민수 등) | MIMIC은 완전 비식별화, 이름 없음 | **D1 확정** — `app.patient_alias` 성씨 마스킹 가명 (`김**`) |
| `bed` / `bedZones` / `bedSummary` / 장비 배지(E·V·C) | 병상 배치 정보 없음 | `app.bed`, `app.bed_assignment` 데모 테이블 |
| `vitals.mental` (Alert 등) | ED 테이블에 의식수준 없음. GCS는 ICU `chartevents` 에만 존재 | **NULL 반환 + TODO**. 적재 시 GCS 3종(220739·223900·223901) 사용 |
| `deteriorationProbability`, `risk`, `trend[].probability` | 모델 산출물 | `app.prediction` 스키마만 준비. **모델 미구현 → TODO** |
| `riskFactors[]`, `recommendations[]` | 모델 산출물 | `app.prediction` JSONB. **TODO** |
| `aiAlerts[]` | 모델 경고 이벤트 | `app.alert`. **TODO** |
| `reassessQueue[].due` ("즉시"/"10분 내") | 재평가 정책 산출물 | 위험도 등급에서 규칙 기반 파생 |
| `currentUser` | 인증 미구현 | Mock 유지 |

### ⛔ 기록 영역 — 범위 밖 (mock 유지, 설계 대상 아님)

| Frontend 데이터 | 처리 |
|---|---|
| `recordStatus` — 환자 목록의 "기록 상태" 컬럼 | **mock 유지** |
| `incompleteRecords` — 대시보드 | `public.clinical_records`와 현재 재실 환자를 조인한 live API 사용 |
| `sampleDialogue`, `aiDraftRecord`, `emptyRecord`, `followUpQuestions` | **mock 유지** |
| `kcdCandidates`, `recordFieldLabels`, `checkStatusMeta`, `outcomeOptions` | **mock 유지** |

---

## 4. 표준 코드 불일치 — 반드시 인지할 것

| 프론트 표기 | MIMIC 실제 | 판단 |
|---|---|---|
| **KTAS Level 1~5** | `triage.acuity` = **ESI(Emergency Severity Index) 1~5** | 둘 다 1=최중증, 5=최경증으로 방향은 같으나 **동일 척도가 아님**. UI 라벨은 R6에 따라 유지하되, API 필드명은 `acuity`로 두고 문서에 각주를 답니다 |
| **KCD-9차 진단코드** | ED `diagnosis.icd_code` = **ICD-9 / ICD-10** | 기록 화면 기능이므로 **범위 밖**. KCD↔ICD 매핑 데이터도 저장소에 없음 → `kcdCandidates` mock 유지 |

---

## 5. 확정 사항 (2026-08-26 승인)

| # | 항목 | **확정 내용** |
|---|---|---|
| **D1** | 환자 이름 | **`app.patient_alias`에 결정론적 가명 저장.** 표기는 **성씨 + 마스킹**(`김**`, `박**`)이며 성씨는 `stay_id` 해시로 배정한 가짜 값. 완전한 실명을 만들지 않아 실존 인물 오해를 피함. 동일 성씨 중복은 정상이며 식별은 `stay_id` 로 한다 |
| **D2** | 병상 현황판 | **`app.bed` 84병상(14구역 × 6) + `app.bed_assignment` 데모 배정 도입. 코호트 전원에게 병상을 주어 환자 목록에 보이는 환자가 현황판에도 보인다(퇴실자는 조회 시점에 빠지며 그 병상은 빈 병상으로 표시).** 위험도 색상·환자명·나이/성별은 실데이터, 구역/병상번호/장비(E·V·C)는 데모. `meta.is_demo_assignment=true`로 구분 |
| **D4** | API 네임스페이스 | **`/api/ed/*` 신설.** 기존 5개 라우터 계약 무손상 (architecture.md §6) |
| **D5** | labevents · chartevents 적재 | **2026-08-31 개정: `load_subset.py` 에 통합 적재.** labevents 는 `subject_id` + ED 시간창(-6h/+24h), chartevents 는 ICU stay + itemid whitelist 14종 (§7.7) |
| **D6** | 데모 시간축 | **`app.demo_stay` 도입** (§6) |
| **D7** | OpenAPI 경로 | **FastAPI 기본 `/docs` 유지** (api-design.md §8) |
| **D8** | 프론트 빌드 타깃 | **`NITRO_PRESET=node-server` 환경변수 우선.** `vite.config.ts` 미변경 (architecture.md §7.4) |
| **TODO** | 모델 output 구조 | **최소 필드 + `detail` JSONB로 진행** (§9.2) |

> D3(진단코드 KCD/ICD 처리)은 기록 영역 제외에 따라 **삭제**되었습니다. 번호는 혼선을 막기 위해 재사용하지 않습니다.

---

## 6. 데모 시간축 (Demo Timeline)

MIMIC의 timestamp는 환자별로 무작위 shift되어 있어(§2.3), "지금 응급실에 있는 환자"라는 개념이 성립하지 않습니다. 하지만 대시보드/모니터링 화면 전체가 그 개념 위에 설계되어 있습니다.

**D6 확정 (rev.4 — 조회 시점 상대값으로 전환)**: 오프셋을 **값으로 저장하지 않습니다.**

초기 구현은 적재 시점의 오프셋을 `demo_offset`/`demo_intime` 으로 고정 저장했는데, 실제 시간이 흐르면 코호트 전체가 과거로 밀려 **19시간 뒤에는 재실 환자가 0명**이 되는 문제가 있었습니다.

지금은 원본 시간축에서 **"현재"에 대응하는 시점(`now_ref`)만** 저장하고, 오프셋은 조회할 때 계산합니다.

```text
app.demo_stay
  ed_stay_id  bigint PK
  now_ref     timestamp   -- 원본 시간축에서 '현재' 에 대응하는 시점 (시간 불변)
  is_active   boolean     -- 코호트에 포함할지

app.v_demo_stay  (조회 시점 계산)
  demo_offset  = now() - now_ref
  demo_intime  = edstays.intime  + demo_offset
  demo_outtime = edstays.outtime + demo_offset
  has_departed = outtime + demo_offset <= now()
```

**now_ref 배치 규칙** (`load_subset.py`):

| 상태 | now_ref 위치 | 결과 |
|---|---|---|
| 재실 중 (목표 75%) | (마지막 vital, 퇴실) 창의 10~90% 지점 | 측정값 전부 과거 · 퇴실 시각은 미래 → "퇴실" 컬럼 빈칸 |
| 퇴실 완료 | `max(퇴실, 마지막 vital) + 5분~8시간` | 모든 관측이 과거 → "퇴실" 유형 표시 |

마지막 vital 이 퇴실보다 늦게 차팅된 stay(원본 데이터 특성, 300건 중 71건)는 재실로 둘 수 없어 자동으로 퇴실 처리됩니다. 실제 결과는 **재실 160 / 퇴실 140**입니다.

### 6.1 데모 시계 (1시간 단위 시연용)

화면의 모든 시각이 한 곳에서 파생되므로, **시계 하나만 움직이면 목록·상세·차트·병상·퇴실 판정이 한꺼번에 따라옵니다.**

```text
app.demo_clock  (1행)
  epoch_virtual   -- 매핑 기준점. now_ref 가 이 가상 시각에 대응한다 (고정)
  anchor_real     -- 시계 보간 기준 (실제)
  anchor_virtual  -- 시계 보간 기준 (가상)
  speed           -- 0=정지, 1=실시간, 3600=1초에 1시간

app.demo_now()   = anchor_virtual + (now() - anchor_real) * speed     ← 흐르는 축
app.demo_epoch() = epoch_virtual                                       ← 고정 기준점
```

| 용도 | 사용하는 함수 |
|---|---|
| 원본 시각 → 화면 시각 변환 (`demo_offset`) | **`demo_epoch()`** |
| "지금 도래했는가" 판정 (`has_departed`, 관측 필터) | **`demo_now()`** |

> ⚠️ **오프셋을 `demo_now()` 로 잡으면 안 됩니다.** `charttime + (demo_now − now_ref) ≤ demo_now` 는 `demo_now` 가 소거되어, 시계를 아무리 진행해도 화면이 그대로입니다. 구현 중 실제로 겪은 오류라 기준점을 분리했습니다.

**관측 필터**: vital·prediction 조회에 `charttime + demo_offset <= demo_now()` 를 겁니다. 시계를 진행하면 데이터가 하나씩 드러나고, 되감기도 정상 동작합니다.

**시연용 배치** (`load_subset.py --demo-start`): `now_ref` 를 첫 측정 직후(`max(intime, 첫 charttime) + 0~30분`)에 둡니다. 전원이 재실 상태로 시작하고, 시계를 진행할수록 vital 이 쌓이고 환자가 퇴실합니다.

실측 결과 (12시간 진행):

| 경과 | 재실 | 퇴실 | 빈병상 | 표본 환자 vital |
|---|---:|---:|---:|---:|
| 기준 | 297 | 3 | 9 | 1 |
| +4h | 257 | 43 | 10 | 3 |
| +8h | 163 | 137 | 17 | 4 |
| +12h | 96 | 204 | 27 | 6 |
| 리셋 | 297 | 3 | 9 | 1 |

리셋은 `epoch_virtual` 까지 되돌리므로 **시나리오가 처음 상태로 복원**됩니다.

**되감기**: `advance?hours=-1` 로 시각을 되돌릴 수 있습니다. 관측 필터가 `demo_now()` 기준이라 데이터가 다시 감춰지고 퇴실이 취소되며, **왕복하면 원래 상태로 정확히 복원**됩니다(+5h → −5h 검증 완료).

되감기 하한은 **시나리오 시작점(`epoch_virtual`)** 입니다. 그 이전으로 가면 `demo_intime` 이 미래가 되어 "아직 도착하지 않은 환자"가 목록에 남기 때문에, `advance()` 에서 `greatest(..., epoch_virtual)` 로 막습니다. 응답의 `can_rewind` 로 UI 버튼을 비활성화합니다.

- 원천 데이터는 **손대지 않습니다.** 상대 시간 간격은 100% 보존됩니다.
- MIMIC 자체의 비식별화 shift와 동일한 성격의 변환이며, 임상값을 만들어내지 않습니다 (R4 위배 아님).
- API 응답에 `is_demo_timeline: true` 플래그를 포함해 혼동을 방지합니다.

---

## 7. 개발용 행(row) 서브셋 전략 — R2

> rev.2에서 **선별 절차를 실행 가능한 수준으로 구체화**했습니다.
> 아래 계층별 가용 건수는 추정이 아니라 **원본 CSV 전수 스캔 실측치**입니다 (2026-08-26 측정).

### 7.1 적격 조건

```text
적격(eligible) = vitalsign 측정 횟수 >= 5
               AND triage.acuity ∈ {1,2,3,4,5}
               AND disposition ∈ {ADMITTED, HOME, EXPIRED}
```

- `vital >= 5`: 상세 화면의 "시간별 상태 변화" 차트가 최소 5개 시점을 필요로 합니다 (mock `trend`도 5포인트).
- `acuity NOT NULL`: 목록의 KTAS 배지가 필수 표시 항목입니다.
- `disposition` 제한: `LEFT WITHOUT BEING SEEN`, `ELOPED`, `TRANSFER`, `OTHER`, `LEFT AGAINST MEDICAL ADVICE`는 악화 여부 해석이 모호하므로 제외합니다.

### 7.2 계층별 적격 풀 — **실측**

| 계층 | 정의 | 적격 건수 | 평균 vital | acuity 1 | 2 | 3 | 4 | 5 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| **A** | `hadm_id ∈ icustays.hadm_id` (ICU 이동 = 악화 양성) | 17,135 | 9.3 | 5,372 | 9,451 | 2,306 | **6** | **0** |
| **B** | `disposition='ADMITTED'` 이면서 ICU 미이동 | 47,670 | 7.7 | 4,667 | 25,451 | 17,358 | 191 | 3 |
| **C** | `disposition='HOME'` (악화 음성) | 40,269 | 7.4 | 2,504 | 18,498 | 18,825 | 434 | 8 |
| **D** | `disposition='EXPIRED'` (ED 사망) | **48** | 9.5 | 21 | 21 | 6 | **0** | **0** |

**두 가지 제약이 실측에서 드러났습니다.**

1. **acuity 4·5는 A·D 계층에 사실상 없습니다** (A: a4=6, a5=0 / D: a4=a5=0). 경증 환자는 ICU로 가지도, ED에서 사망하지도 않으므로 당연한 결과입니다.
   → **계층별 acuity 전수 커버는 불가능**합니다. 대신 **코호트 전체 기준으로 acuity 1~5가 모두 포함되도록** 설계합니다.
2. **계층 D의 적격 풀이 48건뿐**입니다. 쿼터 20건은 풀의 42%에 해당하며, `vital >= 5` 조건을 올리면 즉시 무너집니다.
   → 조건 변경 시 **D부터 재검증**해야 합니다.

### 7.3 코호트: ED stay **300건** — acuity 쿼터 확정

> **현재 DB 는 이 쿼터로 적재되어 있지 않습니다.** 데모 데이터셋으로 전환하면서
> 쿼터 없이 적격 전량 83건을 선별합니다 (§0-1). 아래 쿼터는 **정식 데이터로
> 되돌릴 때** 쓰는 구성이며 `select_cohort.py` 의 `QUOTA` 에 그대로 남아 있습니다.

| 계층 | acuity 1 | 2 | 3 | 4 | 5 | 계층 합 |
|---|---:|---:|---:|---:|---:|---:|
| A (ICU 이동) | 32 | 50 | 16 | 2 | 0 | **100** |
| B (입원, ICU 미이동) | 8 | 36 | 24 | 2 | 0 | **70** |
| C (귀가) | 8 | 48 | 48 | 4 | 2 | **110** |
| D (ED 사망) | 8 | 8 | 4 | 0 | 0 | **20** |
| **acuity 합** | **56** | **142** | **92** | **8** | **2** | **300** |

- 각 쿼터는 §7.2 실측 풀 안에서 **모두 충족 가능**합니다 (최소 여유: A-a4 = 2/6, C-a5 = 2/8, D-a3 = 4/6).
- 계층별 비율은 해당 계층의 실제 acuity 분포를 대체로 따르되, **acuity 3·4·5가 완전히 사라지지 않도록** 하한을 걸었습니다.
- 코호트 전체에서 **acuity 1~5가 모두 등장**합니다 → 목록 화면의 KTAS 배지 5색이 전부 렌더됩니다.

> A 계층 비중이 33%로 실제 유병률(적격 ED stay 중 약 16%)보다 높습니다. 이는 **데모 화면에 고위험 환자가 보이게 하려는 의도적 오버샘플링**이며, 모델 학습용 분포가 아닙니다. 문서와 API `meta`에 명시합니다.

### 7.4 선별 알고리즘 (`database/scripts/select_cohort.py`)

```text
Step 1  vitalsign.csv.gz 스트리밍 → vital_count[stay_id]        (1,564,610행)
Step 2  triage.csv.gz    스트리밍 → acuity[stay_id]             (425,087행)
          · float 문자열("2.0000") → int 캐스팅, 실패 시 제외
Step 3  icustays.csv.gz  스트리밍 → icu_hadm 집합
          ⚠ hadm_id 는 2번째 컬럼(index 1). stay_id(index 2)와 혼동 금지
Step 4  edstays.csv.gz   스트리밍 → 적격 후보 분류 (§7.1 조건)
          tier = A if hadm_id in icu_hadm
                 B if disposition == 'ADMITTED'
                 C if disposition == 'HOME'
                 D if disposition == 'EXPIRED'
                 else 제외
Step 5  결정론적 정렬키
          rank_key = md5(f"{SEED}:{stay_id}").hexdigest()      # SEED = 20260826
          → 같은 입력 = 같은 코호트. 난수 상태에 의존하지 않음
Step 6  (tier, acuity) 버킷별로 rank_key 오름차순 상위 N건 선택
Step 7  쿼터 미달 시 fallback
          같은 tier 내 인접 acuity에서 보충 → 그래도 부족하면 경고 후 진행
          (코호트 총계가 300 미만이면 명시적으로 보고)
Step 8  app.cohort 테이블에 기록 (TRUNCATE 후 COPY)
          ed_stay_id, subject_id, hadm_id, tier, acuity, vital_count, seed
```

**모든 CSV는 Python `csv` 모듈로 파싱합니다.** 문자열 split은 금지입니다 — `chiefcomplaint`에 콤마가 포함됩니다 (§2.3).

**비용**: Step 1~4는 `vitalsign(24MB) + triage(9.5MB) + icustays(3.2MB) + edstays(9.8MB) ≈ 47 MB gz` 스트리밍 스캔입니다. **실측 1~2분.**

**코호트를 DB 에 두는 이유**: 파일(`cohort.csv`)에 두면 `.gitignore` 에 걸려 저장소에 남지 않으므로 팀원 간 코호트가 어긋날 수 있고, DB 만 봐서는 어떤 기준으로 뽑힌 환자인지 알 수 없습니다. `app.cohort` 에 두면 tier·acuity·vital_count·seed 가 DB 안에서 조회 가능합니다.

```sql
SELECT tier, count(*) FROM app.cohort GROUP BY tier;   -- A 100 · B 70 · C 110 · D 20
```

**재현성**: `app.cohort` 가 채워지면 이후 적재는 이 테이블만 참조합니다. `load_subset.py` 의 TRUNCATE 대상에서 제외되어 **재적재해도 코호트가 보존**됩니다. `COHORT_SEED` 를 바꾸지 않는 한 `select_cohort.py` 를 다시 돌려도 결과가 같습니다.

**정합성 검증**: 적재 후 `app.cohort` 의 모든 stay 가 `mimic.edstays` 에 들어갔는지 확인합니다 (FK 는 걸지 않습니다 — 코호트가 edstays 보다 먼저 채워지고, `TRUNCATE ... CASCADE` 가 코호트까지 지워버리기 때문입니다).

### 7.5 예상 적재 행 수

| 테이블 | 필터 | 예상 행 수 | 산출 근거 |
|---|---|---:|---|
| `mimic.edstays` | 코호트 stay_id | 300 | 정의 |
| `mimic.triage` | 코호트 stay_id | 300 | stay당 1행 |
| `mimic.ed_vitalsign` | 코호트 stay_id | **≈ 2,470** | A 100×9.3 + B 70×7.7 + C 110×7.4 + D 20×9.5 |
| `mimic.ed_diagnosis` | 코호트 stay_id | ≈ 600 – 900 | 전체 평균 2.1건/stay |
| `mimic.patients` | 코호트 subject_id | ≤ 300 | 동일 환자 중복 시 감소 |
| `mimic.admissions` | 코호트 hadm_id | ≈ 170 – 190 | A 100 전건 + B 70 대부분 |
| `mimic.icustays` | 코호트 hadm_id | ≈ 100 – 130 | A 계층 hadm당 1개 이상 |
| `app.*` (데모·예측) | 생성 | ≈ 2,000 | bed 84 + assignment 83 + demo_stay 300 + 예측 |
| | | **≈ 6,000 – 6,700행** | |

**예상 DB 용량: 인덱스 포함 20 MB 미만.** 원본 64 GB의 0.03% 수준입니다.

> **`mimic.ed_diagnosis` 취급**: 진단 목록을 보여주는 화면은 기록 화면뿐이며 그쪽은 범위 밖입니다.
> 다만 코호트 기준 600~900행에 불과하고 **향후 모델 feature 후보**(요청서 기준 5)이므로 **적재는 하되 API로 노출하지 않습니다.**

### 7.6 적재 후 검증 (`load_subset.py` 종료 시 자동 실행)

```sql
-- 1) 코호트 규모
SELECT count(*) FROM mimic.edstays;                      -- expect 300

-- 2) acuity 분포 — 1~5가 모두 존재해야 함
SELECT acuity, count(*) FROM mimic.triage GROUP BY acuity ORDER BY acuity;
-- expect 1:56  2:142  3:92  4:8  5:2

-- 3) vital 최소 개수 — 위반 행이 0이어야 함
SELECT stay_id, count(*) c FROM mimic.ed_vitalsign
 GROUP BY stay_id HAVING count(*) < 5;                   -- expect 0 rows

-- 4) 고아 행 — 전부 0이어야 함
SELECT count(*) FROM mimic.ed_vitalsign v
 WHERE NOT EXISTS (SELECT 1 FROM mimic.edstays e WHERE e.stay_id = v.stay_id);
SELECT count(*) FROM mimic.edstays e
 WHERE NOT EXISTS (SELECT 1 FROM mimic.patients p WHERE p.subject_id = e.subject_id);
SELECT count(*) FROM mimic.edstays e
 WHERE e.hadm_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM mimic.admissions a WHERE a.hadm_id = e.hadm_id);

-- 5) 계층 A 무결성 — ICU 이동 100건이 모두 연결되어야 함
SELECT count(DISTINCT e.stay_id) FROM mimic.edstays e
  JOIN mimic.icustays i ON i.hadm_id = e.hadm_id;        -- expect 100

-- 6) 이상치 잔존 확인 (원본 그대로 적재하므로 0이 아닐 수 있음 — 기록용)
SELECT count(*) FROM mimic.ed_vitalsign
 WHERE sbp > 300 OR heartrate > 300 OR o2sat > 100;
```

하나라도 기대값과 다르면 **적재 실패로 처리하고 중단**합니다. 조용히 넘어가지 않습니다.

### 7.7 Phase 구분

| Phase | 대상 | 정식 데이터 비용 | 데모 데이터 비용 |
|---|---|---|---|
| **A (적재 완료)** | ED 4종 + patients + admissions + icustays | 약 120 MB gz · 수 분 | **1초 미만** |
| **B (labevents 적재 완료)** | labevents | 전량 스캔 **116초 (실측)** | **0.4초 (실측)** |
| **B (미적재)** | d_labitems, inputevents, procedureevents | — | — |
| **C (chartevents 적재 완료)** | chartevents | 전량 스캔 **215초 (실측)** | **0.3초 (실측)** |
| **C (미적재)** | d_items | — | — |

> **비용 추정치 정정 (2026-08-28).** 이전 개정은 Phase B 를 15~30분, Phase C 를
> 1시간 이상으로 추정하고 그 비용을 근거로 두 Phase 를 보류했습니다. 실제로 전량
> 스캔을 돌려본 결과 **정식 데이터에서도 각각 116초 / 215초**였습니다. 추정이
> 과대했으며, **"스캔 비용 과다"는 더 이상 보류 사유가 아닙니다.**
>
> 데모 데이터셋에서는 두 파일 합쳐 **0.7초**입니다. Phase 를 분리해 별도 스크립트로
> 둘 실익이 없으므로, 적재하기로 결정되면 `load_subset.py` 에 통합합니다.

**2026-08-31 개정 — labevents · chartevents 적재 확정**

모델·서비스 데이터 검증을 위해 두 테이블을 `load_subset.py` 에 통합했습니다.
UI 표시 영역 유무는 더 이상 적재 판단 기준이 아닙니다. 적재 방식은 다음과 같습니다.

| 테이블 | 필터 | 컬럼 | 데모 실측 행수 |
|---|---|---|---:|
| `mimic.labevents` | `subject_id` ∈ 코호트 34명 **(시간창 없음 · 전체 이력)** | `labevent_id`(PK)·`subject_id`·`hadm_id`·`itemid`·`charttime`·`storetime`·`valuenum` | **75,312** |
| `mimic.chartevents` | `stay_id` ∈ `mimic.icustays` 34건 **AND** `itemid` ∈ whitelist **16종** (`bundle.json` 15종 + `CHARTEVENTS_EXTRA_ITEMS` 의 `223835`) | `id`(BIGSERIAL PK)·`icu_stay_id`·`subject_id`·`hadm_id`·`itemid`·`charttime`·`valuenum` | **27,637** |

> **2026-09-01 개정 — `labevents` 의 시간창을 폐지했습니다.**
> 초기에는 stay 별 [`intime`-6h, `outtime`+24h] 로 잘라 9,272행만 적재했습니다.
> 그런데 모델의 `lab_*_dt` / `lab_*_last` feature 는 "환자의 **마지막 검사가 언제였나**" 를
> 보는 값이고, 학습 분포상 그 간격의 중앙값이 약 95일 · 99분위가 약 5.6년입니다
> (`artifacts/feature_spec.json`). 체류 구간 근처만 적재하면 참조 대상이 더 과거의 검사로
> 밀려 학습 배치와 값이 어긋납니다 — **에러 없이 성능만 조용히 떨어집니다.**
> 관측 시점 컷오프(`storetime <= t`)는 DB 가 아니라 feature layer 가 적용합니다.
>
> 같은 이유로 `labevents` 에는 **itemid 화이트리스트도 걸지 않습니다.** 걸어두면 모델
> 개정으로 필요한 검사가 늘 때 조용히 결측이 됩니다.
>
> `chartevents` 의 whitelist 는 유지하되 목록을 코드에 적지 않고
> `artifacts/bundle.json["vital_itemids"]`(15종)에서 읽고, 여기에 `load_subset.py` 의
> `CHARTEVENTS_EXTRA_ITEMS`(FiO2 `223835`)를 더해 **16종**이 됩니다. 두 곳에 적어두면
> 어긋납니다(실제로 `224690` 이 빠져 있었습니다).
>
> riskmodel 의 100개 feature 중 **36개가 `lab_*`** 입니다. 이 테이블이 비어 있으면
> 예측은 나오지만 근거가 크게 빈 상태가 됩니다 — 배포 시 반드시 적재합니다
> (`docs/oci-deployment.md`).

- **시간축**: 두 테이블 모두 `edstays.intime` 기준 상대시간 계산이 가능합니다
  (`labevents.charttime`/`storetime`, `chartevents.charttime`).
- **chartevents itemid whitelist**(`load_subset.py` `CHARTEVENTS_ITEMS`): vital 5종에 대응하는
  `220045`(HR) · `220179/220180/220181`(NBP) · `220050/220051`(ABP) · `220210`(RR) ·
  `220277`(SpO2) · `223761/223762`(Temp) · `223835`(FiO2) 와, §3 이 `vitals.mental` 용으로
  지정한 GCS 3종 `220739`·`223900`·`223901`. 근거 없는 itemid 는 넣지 않습니다.
  whitelist 없이 코호트 전량은 176,661행이며 그중 59%가 `valuenum` NULL(텍스트 이벤트)입니다.
- **`labevents.hadm_id` 에는 FK 를 걸지 않습니다.** 적재분의 41%(3,809행)가 NULL 이고,
  정식 데이터에서는 시간창에 코호트 밖 입원의 검사가 걸릴 수 있어 FK 가 적재를 깨뜨립니다.
  고아 행 수는 `load_subset.py` 검증에서 기록만 합니다(데모 실측 0건).
- **`d_labitems` / `d_items` 사전은 아직 미적재**입니다. `itemid` 를 라벨로 해석해야 하는
  요구가 생기면 같은 방식으로 추가합니다.

**Phase B/C 적재 시 알아야 할 것**

- `labevents.hadm_id` 로 필터하면 안 됩니다. 정식 데이터 실측 기준 코호트에 걸린
  599,358행 중 **248,092행(41.4%)이 `hadm_id` NULL** 입니다 — 응급실에서 귀가한
  환자(계층 C)의 검사가 입원 건에 묶이지 않기 때문입니다. **`subject_id` + `charttime`
  시간창**으로 필터해야 합니다.
- `d_labitems`(labevents) 와 `d_items`(chartevents) 사전을 함께 적재해야 `itemid` 를
  라벨로 해석할 수 있습니다. 데모 데이터셋에서 두 사전의 **itemid 커버리지는 100%** 입니다
  (chartevents 1,318종 · labevents 498종 전부 수록).
- `mimic.edstays` 를 `TRUNCATE ... CASCADE` 하는 Phase A 재적재는 FK 로 연결된
  Phase B/C 테이블까지 함께 비웁니다. 재적재 후에는 Phase B/C 도 다시 실행해야 합니다.

---

## 8. ERD

> **2026-09-02 갱신 — 로컬 개발 DB(`eron-postgres`) 실측 기준입니다.**
> `information_schema` 의 컬럼·PK 와 `pg_constraint` 의 FK 를 그대로 옮겼습니다.
> 표기: `PK` 기본키 · `FK→` 외래키 · `(n)` nullable · `※` FK 를 걸지 않은 논리적 참조.

### 8.1 `mimic` — MIMIC-IV 서브셋 (읽기 전용)

```text
                    ┌────────────────────────────┐
                    │ mimic.patients             │
                    │ PK subject_id      bigint  │
                    │    gender          char    │
                    │    anchor_age      smallint│
                    │    anchor_year     smallint│
                    │    anchor_year_group text  │
                    │    dod             date    │
                    └──────┬──────────────┬──────┘
                         1 │            1 │
              ┌────────────┘              └──────────────┐
            N │                                        N │
┌─────────────▼──────────────┐              ┌────────────▼───────────────┐
│ mimic.admissions           │            1 │ mimic.labevents            │
│ PK hadm_id         bigint  │◄─────────┐   │ PK labevent_id     bigint  │
│    subject_id      bigint ※│          │   │ FK→patients.subject_id     │
│    admittime/dischtime     │          │   │    hadm_id      bigint (n)※│
│    deathtime          (n)  │          │   │    itemid          int     │
│    admission_type/location │          │   │    charttime   timestamp   │
│    discharge_location      │          │   │    storetime   timestamp(n)│
│    insurance/marital/race  │          │   │    valuenum      float8 (n)│
│    edregtime/edouttime     │          │   └────────────────────────────┘
│    hospital_expire_flag    │          │   ※ hadm_id 에 FK 를 걸지 않는다(§7.7)
└──────┬───────────────┬─────┘          │
     1 │             1 │                │
       │               └────────────┐   │
       │                          N │   │
┌──────▼─────────────────────┐  ┌───▼───▼────────────────────┐
│ mimic.edstays              │  │ mimic.icustays             │
│ PK stay_id         bigint  │  │ PK icu_stay_id     bigint  │
│ FK→patients.subject_id     │  │    subject_id      bigint ※│
│ FK→admissions.hadm_id  (n) │  │ FK→admissions.hadm_id      │
│    intime/outtime timestamp│  │    first/last_careunit     │
│    gender          char    │  │    intime/outtime timestamp│
│    race                    │  │    los            float8   │
│    arrival_transport       │  └───────────┬────────────────┘
│    disposition             │            1 │
└──┬────────┬────────┬───────┘              │ N
 1 │      N │      N │              ┌───────▼────────────────────┐
   │        │        │              │ mimic.chartevents          │
   │        │        │              │ PK id           bigserial  │
   │        │        │              │ FK→icustays.icu_stay_id    │
   │        │        │              │    subject_id/hadm_id     ※│
   │        │        │              │    itemid          int     │
   │        │        │              │    charttime   timestamp   │
   │        │        │              │    valuenum      float8 (n)│
   │        │        │              └────────────────────────────┘
   │        │        │              itemid whitelist 16종 (§7.7)
   │        │        │
┌──▼──────────────┐ ┌▼───────────────────┐ ┌▼──────────────────────┐
│ mimic.triage    │ │ mimic.ed_vitalsign │ │ mimic.ed_diagnosis    │
│ PK stay_id      │ │ PK id    bigserial │ │ PK (stay_id, seq_num) │
│ FK→edstays      │ │ FK→edstays.stay_id │ │ FK→edstays.stay_id    │
│    subject_id  ※│ │    subject_id     ※│ │    subject_id        ※│
│    temperature  │ │    charttime       │ │    icd_code    text   │
│    heartrate    │ │    temperature     │ │    icd_version smallint│
│    resprate     │ │    heartrate       │ │    icd_title   text   │
│    o2sat        │ │    resprate        │ └───────────────────────┘
│    sbp / dbp    │ │    o2sat           │
│    pain    text │ │    sbp / dbp       │
│    acuity smallint│ │    rhythm   text  │
│    chiefcomplaint│ │    pain     text   │
└─────────────────┘ └────────────────────┘
```

`triage.pain` 과 `ed_vitalsign.pain` 이 `text` 인 이유는 §2.3 (원본에 `"7-8"`, `"denies"`
같은 값이 섞여 있다) 을 참고하세요.

### 8.2 `app` — 애플리케이션 데이터 (읽기·쓰기)

```text
        mimic.edstays.stay_id ◄──── FK 로 참조하는 5개 테이블 ────┐
                                                                  │
┌──────────────────────────────┐  ┌──────────────────────────────┐│
│ app.prediction               │  │ app.prediction_ack           ││
│ PK id             bigserial  │  │ PK (ed_stay_id,              ││
│ FK→edstays.stay_id ──────────┼──┼─►    prediction_time)        ││
│    model_version    text     │  │    ed_stay_id      bigint   ※││
│    prediction_time  timestamp│  │    prediction_time timestamp ││
│    t_idx            int      │  │    acknowledged_at timestamp ││
│    horizon_minutes  int      │  │    acknowledged_demo_at      ││
│    risk_probability float8   │  │    acknowledged_by  text (n) ││
│    risk_level       text     │  │    created_at   timestamp    ││
│    detail           jsonb    │  └──────────────────────────────┘│
│    created_at    timestamp   │  ※ 확인 상태만 담는다. FK 없음.   │
└──────────────────────────────┘                                  │
  detail 에 모델이 만든 근거 문장이 그대로 들어간다(§9.2)          │
                                                                  │
┌──────────────────────────────┐  ┌──────────────────────────────┐│
│ app.cohort                   │  │ app.patient_alias            ││
│ PK ed_stay_id      bigint  ※ │  │ PK ed_stay_id      bigint ───┼┤
│    subject_id      bigint    │  │    display_name    text      ││
│    hadm_id      bigint (n)   │  │    is_pseudonym    bool      ││
│    tier            char      │  └──────────────────────────────┘│
│    acuity          smallint  │                                  │
│    vital_count     int       │  ┌──────────────────────────────┐│
│    seed            text      │  │ app.demo_stay                ││
│    selected_at  timestamp    │  │ PK ed_stay_id      bigint ───┼┤
└──────────────────────────────┘  │    now_ref      timestamp    ││
  ※ 예측 대상 83건의 정본.         │    is_active       bool      ││
    FK 없음(선별이 적재보다 앞선다) └──────────────────────────────┘│
                                                                  │
┌──────────────────────────────┐  ┌──────────────────────────────┐│
│ app.bed                      │  │ app.alert                    ││
│ PK bed_id  text (A01…N06)    │  │ PK id            bigserial   ││
│    zone            text      │  │ FK→edstays.stay_id ──────────┼┘
│    sort_order      int       │  │    alert_time   timestamp    │
└───────────┬──────────────────┘  │    level / message   text    │
          1 │                     │    acknowledged_at/_by   (n) │
          N │                     └──────────────────────────────┘
┌───────────▼──────────────────┐    현재 경고는 app.prediction 에서
│ app.bed_assignment           │    조회 시점에 파생한다(§9.2 주석).
│ PK id             bigserial  │
│ FK→bed.bed_id                │  ┌──────────────────────────────┐
│ FK→edstays.stay_id      (n)  │  │ app.demo_clock  (1행 고정)    │
│    devices        text[]     │  │ PK id            smallint    │
│    assigned_at  timestamp    │  │    epoch_virtual timestamp   │
│    released_at  timestamp(n) │  │    anchor_real   timestamp   │
└──────────────────────────────┘  │    anchor_virtual timestamp  │
                                  │    speed          numeric    │
                                  │    updated_at   timestamp    │
                                  └──────────────────────────────┘
                                    데모 시계. app.demo_now() 의 원천(§6.1)
```

`app` 스키마의 뷰는 §11 에서 다룹니다.

| 뷰 | 컬럼 |
|---|---|
| `app.v_demo_stay` | `ed_stay_id`, `is_active`, `now_ref`, `demo_offset`(interval), `demo_intime`, `demo_outtime`, `has_departed` |
| `app.v_latest_prediction` | `stay_id`, `prediction_time`, `risk_probability`, `risk_level`, `detail`, `model_version` |
| `app.v_latest_vitalsign` | `stay_id`, `measured_at`, `heartrate`, `resprate`, `sbp`, `dbp`, `o2sat`, `temperature_c` |
| `mimic.v_ed_vitalsign_clean` | `ed_vitalsign` + `temperature_c`(°F→°C 변환) |

> `demo_offset` · `demo_intime` 은 테이블이 아니라 **`app.v_demo_stay` 뷰의 계산 컬럼**입니다.
> 테이블 `app.demo_stay` 가 실제로 저장하는 것은 `now_ref` 와 `is_active` 뿐입니다.

### 8.3 `public` — 기존 CRUD 도메인 (별개 자원)

`/api/ed/*` 가 다루는 MIMIC ED stay 와 **다른 도메인**입니다(§6 D4). 서로 FK 로 연결되지 않습니다.

```text
┌───────────────────────┐
│ public.patients       │        ┌──────────────────────────────┐
│ PK id           serial│        │ public.clinical_records      │
│    patient_number     │        │ PK id             serial     │
│    name / gender      │        │    ed_stay_id   varchar    ※ │
│    birth_date   date  │        │    status       varchar      │
│    created_at         │        │    record_payload    json    │
└───────────┬───────────┘        │    selected_kcd      json    │
          1 │                    │    clinician_id/_name        │
          N │                    │    signed_by / signed_at (n) │
┌───────────▼───────────┐        │    created_at / updated_at   │
│ public.visits         │        └──────────────────────────────┘
│ PK id           serial│        ※ mimic.edstays.stay_id 를 문자열로
│ FK→patients.id        │           담는 임시 연결 키. FK 없음.
│    arrival_time       │           (docs/clinical-record-persistence.md)
│    triage_level  int  │
│    chief_complaint    │        ┌──────────────────────────────┐
│    status  varchar    │        │ public.kcd_codes             │
│    created_at         │        │ PK id             serial     │
└──┬────────┬────────┬──┘        │    code        varchar       │
 N │      N │      N │           │    name_ko / name_en         │
   │        │        │           └──────────────────────────────┘
┌──▼──────┐ ┌▼──────────┐ ┌▼──────────────┐   현재 0행(미적재)
│ vitals  │ │predictions│ │ records       │
│ PK id   │ │ PK id     │ │ PK id         │
│ FK→visits│ │ FK→visits │ │ FK→visits     │
│ measured_at│ predicted_at│ record_type   │
│ heart_rate│ risk_score │ │ content  text │
│ respiratory_rate│ risk_level│ generated_by│
│ systolic_bp│ prediction_horizon│ created_at│
│ diastolic_bp│ risk_factors│ confirmed_at │
│ temperature│           │ │               │
│ spo2 / consciousness│   │ └───────────────┘
└─────────┘ └───────────┘
```

`public.clinical_records` 와 `public.kcd_codes` 는 `database/init/*.sql` 이 아니라
backend 기동 시 `Base.metadata.create_all` 이 만듭니다(§9.3).

> ⚠ 로컬 DB 에는 `public.test_connection`(`id`, `message`) 이 남아 있습니다. 초기 연결
> 확인용 잔재이며 애플리케이션이 사용하지 않습니다. 설계 대상이 아니므로 ERD 에서
> 제외했고, 정리 대상으로만 적어 둡니다.

> ⛔ `app.ed_record` 는 기록 영역 제외 결정으로 설계에서 삭제되었습니다. 이후 기록
> 저장은 위의 `public.clinical_records` 로 구현되었습니다.

---

## 9. 테이블 스키마 (DDL 초안)

> 아래는 **설계 초안**입니다. 승인 전 실행하지 않습니다.

### 9.1 `mimic` 스키마

```sql
CREATE SCHEMA mimic;

-- HOSP.patients
CREATE TABLE mimic.patients (
    subject_id        BIGINT       PRIMARY KEY,
    gender            CHAR(1),
    anchor_age        SMALLINT,
    anchor_year       SMALLINT,
    anchor_year_group TEXT,
    dod               DATE
);

-- HOSP.admissions
CREATE TABLE mimic.admissions (
    hadm_id               BIGINT    PRIMARY KEY,
    subject_id            BIGINT    NOT NULL REFERENCES mimic.patients(subject_id),
    admittime             TIMESTAMP,
    dischtime             TIMESTAMP,
    deathtime             TIMESTAMP,
    admission_type        TEXT,
    admission_location    TEXT,
    discharge_location    TEXT,
    insurance             TEXT,
    marital_status        TEXT,
    race                  TEXT,
    edregtime             TIMESTAMP,
    edouttime             TIMESTAMP,
    hospital_expire_flag  SMALLINT
);
-- 제외 컬럼: admit_provider_id, language (화면 요구 없음)

-- ED.edstays
CREATE TABLE mimic.edstays (
    stay_id           BIGINT     PRIMARY KEY,
    subject_id        BIGINT     NOT NULL REFERENCES mimic.patients(subject_id),
    hadm_id           BIGINT     REFERENCES mimic.admissions(hadm_id),  -- 52.2% NULL
    intime            TIMESTAMP  NOT NULL,
    outtime           TIMESTAMP,
    gender            CHAR(1),
    race              TEXT,
    arrival_transport TEXT,
    disposition       TEXT
);

-- ED.triage  (stay당 1행)
CREATE TABLE mimic.triage (
    stay_id        BIGINT  PRIMARY KEY REFERENCES mimic.edstays(stay_id),
    subject_id     BIGINT  NOT NULL,
    temperature    DOUBLE PRECISION,   -- °F
    heartrate      DOUBLE PRECISION,
    resprate       DOUBLE PRECISION,
    o2sat          DOUBLE PRECISION,
    sbp            DOUBLE PRECISION,
    dbp            DOUBLE PRECISION,
    pain           TEXT,               -- 자유텍스트 3.6% 존재
    acuity         SMALLINT,           -- ESI 1~5, 1.64% NULL
    chiefcomplaint TEXT
);

-- ED.vitalsign
CREATE TABLE mimic.ed_vitalsign (
    id          BIGSERIAL  PRIMARY KEY,
    stay_id     BIGINT     NOT NULL REFERENCES mimic.edstays(stay_id),
    subject_id  BIGINT     NOT NULL,
    charttime   TIMESTAMP  NOT NULL,
    temperature DOUBLE PRECISION,      -- °F, 36.1% NULL
    heartrate   DOUBLE PRECISION,
    resprate    DOUBLE PRECISION,
    o2sat       DOUBLE PRECISION,
    sbp         DOUBLE PRECISION,
    dbp         DOUBLE PRECISION,
    rhythm      TEXT,                  -- 96.2% NULL
    pain        TEXT
);

-- ED.diagnosis
CREATE TABLE mimic.ed_diagnosis (
    stay_id     BIGINT   NOT NULL REFERENCES mimic.edstays(stay_id),
    seq_num     SMALLINT NOT NULL,
    subject_id  BIGINT   NOT NULL,
    icd_code    TEXT     NOT NULL,
    icd_version SMALLINT NOT NULL,
    icd_title   TEXT,
    PRIMARY KEY (stay_id, seq_num)
);

-- ICU.icustays  (원본 stay_id는 ED stay_id와 다른 네임스페이스 → 이름 분리)
CREATE TABLE mimic.icustays (
    icu_stay_id    BIGINT  PRIMARY KEY,
    subject_id     BIGINT  NOT NULL,
    hadm_id        BIGINT  NOT NULL REFERENCES mimic.admissions(hadm_id),
    first_careunit TEXT,
    last_careunit  TEXT,
    intime         TIMESTAMP,
    outtime        TIMESTAMP,
    los            DOUBLE PRECISION
);

-- 검사 결과. riskmodel 의 lab feature 36개가 여기서 나온다.
-- 🔑 시간창으로 자르지 않는다(§7.7). itemid 화이트리스트도 걸지 않는다.
-- hadm_id 에 FK 를 걸지 않는 이유도 §7.7 참조.
CREATE TABLE mimic.labevents (
    labevent_id BIGINT PRIMARY KEY,
    subject_id  BIGINT    NOT NULL,
    hadm_id     BIGINT,
    itemid      INTEGER   NOT NULL,
    charttime   TIMESTAMP NOT NULL,   -- 채혈 시각
    storetime   TIMESTAMP,            -- 결과 보고 시각. feature 는 이쪽을 쓴다
    valuenum    DOUBLE PRECISION
);

-- ICU 활력징후. ED 퇴실 후 구간을 메우는 보조 원천이다(커버리지 낮음).
-- itemid 목록은 artifacts/bundle.json["vital_itemids"] 가 정본이다.
CREATE TABLE mimic.chartevents (
    id          BIGSERIAL PRIMARY KEY,
    icu_stay_id BIGINT    NOT NULL,
    subject_id  BIGINT    NOT NULL,
    hadm_id     BIGINT    NOT NULL,
    itemid      INTEGER   NOT NULL,
    charttime   TIMESTAMP NOT NULL,
    valuenum    DOUBLE PRECISION
);
```

> ⚠️ **이름 충돌 주의**: MIMIC-IV-ED의 `stay_id`와 MIMIC-IV-ICU의 `stay_id`는 **서로 다른 식별자 체계**입니다. ICU 측을 `icu_stay_id`로 명시적으로 분리했습니다.

### 9.2 `app` 스키마

```sql
CREATE SCHEMA app;

-- 예측 결과 (모델 output 구조 미확인 → 최소 필드 + TODO)
CREATE TABLE app.prediction (
    id                BIGSERIAL  PRIMARY KEY,
    ed_stay_id        BIGINT     NOT NULL REFERENCES mimic.edstays(stay_id),
    model_version     TEXT       NOT NULL,
    prediction_time   TIMESTAMP  NOT NULL,   -- 예측 기준 시각
    t_idx             INTEGER,               -- 시간축 인덱스 (모델이 사용할 경우)
    horizon_minutes   INTEGER,               -- 예: 360 = P(악화 in (t, t+6h])
    risk_probability  DOUBLE PRECISION NOT NULL CHECK (risk_probability BETWEEN 0 AND 1),
    risk_level        TEXT       NOT NULL,   -- stable|watch|rising|critical
    detail            JSONB,                 -- TODO: 모델 확정 후 정규화
    created_at        TIMESTAMP  NOT NULL DEFAULT now(),
    UNIQUE (ed_stay_id, model_version, prediction_time)
);
```

**TODO — 모델 output 구조를 저장소에서 확인할 수 없어 확정하지 못한 항목 (R4):**

```text
TODO  outcome별 확률 분리 여부
      (respiratory_probability / vasopressor_probability /
       death_probability / cpr_probability)
      → 확정 시 detail JSONB에서 실제 컬럼으로 승격
TODO  riskFactors[] 의 스키마 (문자열 배열? SHAP 기여도 포함 객체?)
TODO  recommendations[] 의 출처 (모델 산출 vs 규칙 기반)
TODO  t_idx 의 시간 간격 (1시간? 15분?)
TODO  horizon_minutes 의 실제 값
근거: 저장소 내 모델 코드·inference 코드·output 스펙 부재.
      .env.example 의 PREDICT_AI_URL 만 존재하고 호출부 없음.
```

```sql
-- 가명 (D1 확정)
CREATE TABLE app.patient_alias (
    ed_stay_id   BIGINT PRIMARY KEY REFERENCES mimic.edstays(stay_id),
    display_name TEXT   NOT NULL,
    is_pseudonym BOOLEAN NOT NULL DEFAULT TRUE
);

-- 데모 시간축
CREATE TABLE app.demo_stay (
    ed_stay_id  BIGINT    PRIMARY KEY REFERENCES mimic.edstays(stay_id),
    demo_offset INTERVAL  NOT NULL,
    demo_intime TIMESTAMP NOT NULL,
    is_active   BOOLEAN   NOT NULL DEFAULT TRUE
);

-- 병상 (D2 확정)
CREATE TABLE app.bed (
    bed_id TEXT PRIMARY KEY,          -- A01 … F06
    zone   TEXT NOT NULL              -- "A 구역 (Resus)" …
);

CREATE TABLE app.bed_assignment (
    id          BIGSERIAL PRIMARY KEY,
    bed_id      TEXT      NOT NULL REFERENCES app.bed(bed_id),
    ed_stay_id  BIGINT    REFERENCES mimic.edstays(stay_id),
    devices     TEXT[]    NOT NULL DEFAULT '{}',   -- {E,V,C}
    assigned_at TIMESTAMP NOT NULL DEFAULT now(),
    released_at TIMESTAMP
);

-- AI 경고 (TODO: 모델 연동 전까지 비어 있음)
CREATE TABLE app.alert (
    id              BIGSERIAL PRIMARY KEY,
    ed_stay_id      BIGINT    NOT NULL REFERENCES mimic.edstays(stay_id),
    alert_time      TIMESTAMP NOT NULL,
    level           TEXT      NOT NULL,
    message         TEXT      NOT NULL,
    acknowledged_at TIMESTAMP,
    acknowledged_by TEXT
);

-- 의료진 "재검토 완료" 확인 상태. (POST /api/ed/alerts/{stay_id}/acknowledge)
--
-- 🔑 경고 자체는 app.prediction 에서 조회 시점에 파생한다(app.alert 는 쓰지 않는다).
--    여기 저장하는 것은 **의료진이 확인했다는 사실** 하나뿐이다.
-- 🔑 PK 에 prediction_time 을 포함하는 이유: 확인은 "그 시점 예측에 대한 확인"이다.
--    다음 예측이 생기면 최신 prediction_time 이 달라져 확인이 자동으로 풀린다.
CREATE TABLE app.prediction_ack (
    ed_stay_id           BIGINT    NOT NULL,
    prediction_time      TIMESTAMP NOT NULL,   -- MIMIC 원본 시간축
    acknowledged_at      TIMESTAMP NOT NULL DEFAULT now(),          -- 감사용 실제 시각
    acknowledged_demo_at TIMESTAMP NOT NULL DEFAULT app.demo_now(), -- 유효성 판정은 이 값
    acknowledged_by      TEXT,
    created_at           TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (ed_stay_id, prediction_time)
);
```

> `acknowledged_demo_at` 이 데모 시각인 이유: 데모 시계를 되돌리면 그보다 나중에 한
> 확인은 '아직 하지 않은 것'이 되어야 합니다. 기록을 지우지 않고 시간 기준으로만
> 무효화하므로, 다시 앞으로 가면 되살아납니다.

> ⛔ **`app.ed_record`는 설계에서 삭제되었습니다.** 기록 영역을 건드리지 않으므로 기록 저장 테이블을 만들지 않습니다.
> 대시보드의 "기록 미완료"는 `public.clinical_records`를 조회하는 live API를 사용합니다.

### 9.3 `public` 스키마

기존 `patients` / `visits` / `vitals` / `predictions` / `records` 5개 테이블은 **변경하지 않습니다.** 기존 CRUD API 계약을 그대로 유지합니다.

**2026-09-01 추가** — 기록 영역이 구현되면서 두 테이블이 늘었습니다.

| 테이블 | 용도 | 참고 |
|---|---|---|
| `public.clinical_records` | 응급진료기록 DRAFT/SIGNED 저장. `ed_stay_id` 를 임시 연결 키로 쓴다 | `docs/clinical-record-persistence.md` |
| `public.kcd_codes` | KCD 진단코드 조회 (`GET /api/kcd/search`) | 적재는 `backend/scripts/import_kcd9.py` |

> ⚠ **이 둘은 `database/init/*.sql` 이 만들지 않습니다.** backend 기동 시
> `Base.metadata.create_all`(`backend/app/main.py`)이 생성합니다. init SQL 은 볼륨이 비어
> 있을 때만 도는 반면 이쪽은 매 기동마다 확인되므로, 스키마를 손으로 맞출 때
> 빠뜨리기 쉽습니다.

---

## 10. 인덱스 설계

실제 API 쿼리 패턴에서 역산했습니다. **모든 컬럼에 인덱스를 걸지 않습니다.**

| # | 인덱스 | 대응 쿼리 |
|---|---|---|
| 1 | `mimic.edstays (stay_id)` — PK | `GET /api/ed/stays/{stay_id}` |
| 2 | `mimic.edstays (subject_id)` | 동일 환자 과거 내원 조회 |
| 3 | `mimic.edstays (hadm_id) WHERE hadm_id IS NOT NULL` — 부분 인덱스 | admissions/icustays 조인. NULL 52.2%를 인덱스에서 제외 |
| 4 | `mimic.edstays (intime DESC)` | 목록 기본 정렬, 페이지네이션 |
| 5 | `mimic.triage (stay_id)` — PK | 1:1 조인 |
| 6 | `mimic.triage (acuity)` | `?acuity=1` 필터 |
| 7 | **`mimic.ed_vitalsign (stay_id, charttime DESC)`** | **가장 중요.** 추이 조회 + "최신 1건" 모두 커버 |
| 8 | `mimic.ed_diagnosis (stay_id)` — PK 선행 컬럼으로 충분, **별도 인덱스 생성 안 함** | (API 미노출. 모델 feature 조회용) |
| 9 | `mimic.icustays (hadm_id)` | ED→ICU 이동 여부 판정 |
| 10 | **`app.prediction (ed_stay_id, prediction_time DESC)`** | 최신 예측 + 확률 추이 |
| 11 | `app.prediction (risk_level)` | `?risk_level=critical` 필터, 대시보드 집계 |
| 12 | `app.alert (alert_time DESC)` | 실시간 경고 목록 |
| 13 | `app.bed_assignment (ed_stay_id) WHERE released_at IS NULL` — 부분 인덱스 | 현재 배정 조회 |

**#7과 #10이 핵심**입니다. `(stay_id, charttime DESC)` 복합 인덱스 하나로 다음 두 패턴을 모두 처리합니다.

```sql
-- 추이 전체
SELECT charttime, heartrate, sbp, dbp, o2sat, temperature
  FROM mimic.ed_vitalsign WHERE stay_id = $1 ORDER BY charttime;

-- 최신 1건
SELECT ... FROM mimic.ed_vitalsign
 WHERE stay_id = $1 ORDER BY charttime DESC LIMIT 1;
```

**의도적으로 만들지 않는 인덱스**: `chiefcomplaint` 전문검색(GIN) — 서브셋 300건에서는 seq scan이 더 빠릅니다. 전체 적재 시 재검토합니다.

---

## 11. View / Summary Table

목록 화면에서 stay마다 "최신 vital 1건"을 뽑는 것이 N+1이 되기 쉬운 지점입니다.

```sql
-- 최신 vital 1건 (LATERAL — 인덱스 #7 활용)
CREATE VIEW app.v_latest_vitalsign AS
SELECT e.stay_id, v.charttime, v.heartrate, v.resprate,
       v.sbp, v.dbp, v.o2sat, v.temperature
  FROM mimic.edstays e
  LEFT JOIN LATERAL (
      SELECT * FROM mimic.ed_vitalsign
       WHERE stay_id = e.stay_id ORDER BY charttime DESC LIMIT 1
  ) v ON TRUE;

-- 최신 예측 1건
CREATE VIEW app.v_latest_prediction AS
SELECT e.stay_id, p.prediction_time, p.risk_probability, p.risk_level, p.detail
  FROM mimic.edstays e
  LEFT JOIN LATERAL (
      SELECT * FROM app.prediction
       WHERE ed_stay_id = e.stay_id ORDER BY prediction_time DESC LIMIT 1
  ) p ON TRUE;

-- 이상치 필터 (§2.3 대응)
CREATE VIEW mimic.v_ed_vitalsign_clean AS
SELECT id, stay_id, charttime,
       NULLIF_OUTSIDE(heartrate,   20, 300)  AS heartrate,
       NULLIF_OUTSIDE(resprate,     4,  80)  AS resprate,
       NULLIF_OUTSIDE(o2sat,       50, 100)  AS o2sat,
       NULLIF_OUTSIDE(sbp,         30, 300)  AS sbp,
       NULLIF_OUTSIDE(dbp,         10, 200)  AS dbp,
       NULLIF_OUTSIDE(temperature, 90, 115)  AS temperature_f
  FROM mimic.ed_vitalsign;
-- NULLIF_OUTSIDE 는 CASE WHEN 로 인라인 전개 (헬퍼 함수 도입은 승인 후 결정)
```

**Materialized view는 도입하지 않습니다.** 서브셋 300 stay / 3천 vital 행 규모에서는 일반 view + 인덱스로 충분하며, 조기 최적화입니다 (요청서 §23). 전체 적재 시 `EXPLAIN ANALYZE` 결과를 근거로 재검토합니다.

---

## 12. CSV → PostgreSQL 적재 방식

애플리케이션에서 행 단위 INSERT는 사용하지 않습니다.

```text
1) select_cohort.py                                    (§7.4 알고리즘)
   vitalsign / triage / icustays / edstays 스트리밍 스캔 → 층화 추출
   → app.cohort 테이블 (COPY)
     (ed_stay_id, subject_id, hadm_id, tier, acuity, vital_count, seed)

2) load_subset.py   (Phase A)
   for each table:
     gzip 스트리밍 읽기 → 코호트 키로 필터 → 필요 컬럼만 → 임시 CSV
     psycopg3  COPY … FROM STDIN (FORMAT csv, HEADER true)
   → 인덱스는 적재 완료 후 생성 (COPY 중 인덱스 유지 비용 회피)
```

- Python `csv` 모듈로 파싱합니다. **문자열 split 금지** — `chiefcomplaint`에 콤마가 포함됩니다 (§2.3).
- 빈 문자열은 `NULL`로 매핑합니다 (`COPY … NULL ''`).
- 각 테이블 적재 후 행 수를 검증하고 로그로 남깁니다.
- `load_full.py`(전체 적재)는 **별도 파일로 분리하며, 승인 없이 실행하지 않습니다** (R2).

### 12.1 DB → DB 이관 — 프로젝트 테이블 재적재 (2026-09-02 추가)

§12 는 **원본 CSV 가 있는 곳**(로컬)에서 쓰는 경로입니다. 서버에는 `MIMIC-DEMO/` 원본을
두지 않으므로, 배포 환경에는 CSV 를 다시 돌리는 대신 **로컬에서 만들어진 결과를 그대로
옮깁니다.** 코호트 선별(`app.cohort`)·데모 시간축(`app.demo_stay`)·이미 계산된
예측(`app.prediction`)은 재현 대상이 아니라 이관 대상이기 때문입니다.

`database/scripts/` 의 셸 스크립트 네 개가 이 경로를 담당합니다.

| 파일 | 하는 일 | DB 변경 |
|---|---|---|
| `project_tables.sh` | 대상 테이블 화이트리스트 **18개**의 단일 정본. 나머지 셋이 이 파일만 읽습니다 | — (변수 정의만) |
| `inspect_project_tables.sh` | 대상 테이블 존재·row 수·시퀀스·데모 상태 조회 | ❌ `SELECT` 만 |
| `dump_project_tables.sh` | 로컬 DB → 재적재용 `backups/eron_project_<stamp>.sql.gz` + 매니페스트 생성 | ❌ 읽기 전용 |
| `restore_project_tables.sh` | 대상 DB 에 그 SQL 을 적용 | ✅ **유일하게 DB 를 바꾸는 스크립트** |

`oci_inspect.sql` 은 같은 조회를 파일 전송 없이 `ssh … psql < oci_inspect.sql` 로
흘려보내기 위한 단독 SQL 입니다. 절차는 `docs/oci-deployment.md` 를 따릅니다.

#### 화이트리스트 18개 — 순서가 곧 FK 부모 → 자식

```text
mimic.patients · mimic.admissions · mimic.edstays · mimic.triage · mimic.ed_vitalsign
mimic.ed_diagnosis · mimic.icustays · mimic.chartevents · mimic.labevents
app.bed · app.cohort · app.demo_clock · app.demo_stay · app.patient_alias
app.prediction · app.prediction_ack · app.alert · app.bed_assignment
```

`pg_dump` 가 뽑는 `COPY` 순서와 같으므로 **FK 를 끈 채로 넣지 않습니다.** 순서나 정합성이
어긋나면 제약이 걸려 전체가 롤백됩니다(부분 적용이 없습니다).

의도적으로 제외한 것과 이유입니다.

| 제외 | 이유 |
|---|---|
| `clinicalnlp.*` | 의료용어·KCD·정책·Vector. 서버에서만 적재하며 로컬에는 없습니다 |
| `public.*` | backend CRUD 도메인(§9.3). 로컬은 스모크 테스트 행뿐이고, 서버에는 실제 `clinical_records` 가 있을 수 있어 **덮어쓰면 안 됩니다** |
| `mimic_ed.*` | 코드가 참조하지 않는 초기 적재 잔재 |
| `public.test_connection` | 초기 연결 확인용 잔재 (§8.3 주석) |

#### 재적재 SQL 의 형태

```sql
BEGIN;
  -- 대상 18개가 전부 있는지 먼저 확인(하나라도 없으면 예외 → 롤백)
  TRUNCATE TABLE <18개 테이블만> RESTART IDENTITY;   -- CASCADE 를 쓰지 않는다
  COPY ...   -- 부모 → 자식 순서, FK 제약을 켠 채로
  setval(...)
COMMIT;
```

**`CASCADE` 를 쓰지 않는 것이 핵심 안전장치입니다.** 화이트리스트 밖 테이블이 대상을
참조하고 있으면 `TRUNCATE` 가 그 자리에서 실패하고 멈춥니다 — 다른 프로젝트 데이터를
조용히 지우는 경로가 없습니다. 그래서 `dump`·`restore` 양쪽이 실행 전에
`pg_constraint` 로 "밖에서 대상을 참조하는 FK" 를 먼저 조회하고, 하나라도 있으면
사람이 판단하도록 중단합니다.

스크립트가 **하지 않는 것**입니다. 코드에 존재하지 않습니다.

```text
DROP DATABASE / DROP SCHEMA / DROP TABLE / TRUNCATE ... CASCADE
docker compose down -v / docker volume rm / PGDATA 초기화
화이트리스트 밖 테이블에 대한 INSERT · UPDATE · DELETE · TRUNCATE
```

#### 매니페스트를 DB 가 아니라 덤프 파일에서 뽑는 이유

행 수를 원본 DB 에 다시 물어보면, 재예측 스케줄러가 `pg_dump` 전후로
`app.prediction` 에 INSERT 한 만큼 실제 적재량과 어긋납니다. 그래서 매니페스트는
**덤프 안의 `COPY` 블록 행 수**를 세서 만듭니다 — 그 값이 곧 적재될 행 수입니다.
`restore` 는 적용 후 이 매니페스트와 대조해 하나라도 다르면 실패로 처리합니다.

같은 이유로 `restore` 는 대상 서버에 `eron-backend` 가 떠 있으면 경고하고 중지를
권합니다(`docker compose stop backend` — 볼륨·데이터는 그대로입니다).

#### 실측 행 수 (2026-09-02 · 로컬 `eron` DB)

| 테이블 | 행 수 | 테이블 | 행 수 |
|---|---:|---|---:|
| `mimic.patients` | 34 | `app.bed` | 84 |
| `mimic.admissions` | 173 | `app.bed_assignment` | 83 |
| `mimic.edstays` | 180 | `app.cohort` | 83 |
| `mimic.triage` | 83 | `app.demo_clock` | 1 |
| `mimic.ed_vitalsign` | 665 | `app.demo_stay` | 83 |
| `mimic.ed_diagnosis` | 191 | `app.patient_alias` | 83 |
| `mimic.icustays` | 34 | `app.prediction` | 801 |
| `mimic.chartevents` | 27,637 | `app.prediction_ack` | 0 |
| `mimic.labevents` | 75,312 | `app.alert` | 0 |

압축 후 약 1.1 MB 입니다. `app.alert` 가 0인 것은 정상입니다 — 경고는 조회 시점에
`app.prediction` 에서 파생하며 이 테이블에 쓰지 않습니다(§9.2).

> `backups/` 는 `.gitignore` 대상입니다. 덤프에는 MIMIC 유래 임상값이 그대로 들어 있으므로
> **커밋하지 않고, 저장소 밖 경로로도 공개하지 않습니다** (§15).

---

## 13. 예상 성능 / 블로커

| 항목 | 평가 |
|---|---|
| Phase A 적재 시간 | ED 4파일 + patients + admissions + icustays ≈ 120 MB gz 스캔. **수 분 이내** |
| Phase A DB 용량 | 인덱스 포함 **< 20 MB** |
| API 응답 시간 | 83~300 stay 규모. 목록/상세 모두 **10 ms 이하** |
| ~~**블로커 1 — labevents**~~ | **적재 완료 (2026-08-31).** 코호트 34명의 **전체 이력 75,312행** (2026-09-01 에 시간창을 폐지했습니다 — §7.7. 최초 적재는 체류 구간으로 자른 9,272행이었습니다). 이하 2026-08-28 기록: 15~30분으로 추정했으나 전량 스캔 실측 **116초**(데모 0.4초). 비용은 보류 사유가 아님. 미적재 사유는 **현 UI 에 lab 표시 영역이 없다**는 것뿐입니다 (§7.7) |
| ~~**블로커 2 — chartevents**~~ | **적재 완료 (2026-08-31).** 코호트 서브셋 **27,637행** (itemid whitelist 를 `bundle.json` 기준 16종으로 맞추며 25,871 → 27,637 로 늘었습니다 — §7.7). `mental`(GCS)은 데이터는 있으나 API 미배선 — 별도 작업. 이하 2026-08-28 기록: 1시간+ 로 추정했으나 전량 스캔 실측 **215초**(데모 0.3초). `mental`(GCS)은 여전히 `null` 이지만, 적재를 막는 것은 비용이 아니라 UI 요구 부재입니다 (§7.7) |
| **블로커 8 — 데모 데이터의 라벨 부재** | 데모 데이터셋에는 `disposition='EXPIRED'` 가 **0건**이라 계층 D 가 존재하지 않고, acuity 5 도 0건입니다. 악화 라벨은 계층 A(ICU 이동) 32건만으로 구성됩니다 (§0-1) |
| **블로커 3 — 모델 output 미확인** | 저장소에 모델·inference 코드 부재. **확정: 최소 필드 + `detail` JSONB로 진행.** 모델 스펙 확정 시 JSONB에서 실컬럼으로 승격 (R4) |
| **블로커 4 — 프론트 미대응 필드** | `name`·`bed`·`devices`는 **D1/D2로 해소**(app 계층 데모). `mental`은 `null` 유지, `riskFactors`·`recommendations`는 모델 확정까지 빈 배열 (§3·§5) |
| **블로커 5 — 현재 재실 개념 부재** | MIMIC 날짜 shift로 "현재 응급실 환자" 성립 불가 → **D6 확정으로 해소.** `app.demo_stay` 시간축 변환 (§6) |
| **블로커 6 — 계층 D 풀이 48건** | `disposition='EXPIRED'` + `vital>=5` + `acuity` 적격 건이 **48건뿐**. 쿼터 20건은 풀의 42%. 적격 조건을 강화하면 즉시 무너지므로, 조건 변경 시 **D부터 재검증** (§7.2) |
| **블로커 7 — acuity 4·5 계층 편중** | 경증(acuity 4·5)은 ICU 이동·ED 사망 계층에 사실상 없음(A: a4=6/a5=0, D: 0/0). 계층별 acuity 전수 커버 불가 → **코호트 전체 기준**으로만 1~5 보장 (§7.2) |
| PostgreSQL / Docker | `docker-compose.yml` 존재, 이미지 `pgvector/pgvector:pg16`. **미기동 상태이며 본 설계 단계에서 실행하지 않았습니다** |

---

## 14. 사용하지 않는 MIMIC 테이블과 근거

| 테이블 | 제외 근거 |
|---|---|
| `hosp/prescriptions` (579 MB) | 처방 화면 없음 |
| `hosp/emar`, `emar_detail` (13.9 GB) | 투약 실행 기록 화면 없음 |
| `hosp/diagnoses_icd` (636만 행) | ED `diagnosis`로 충분. 입원 청구 진단은 화면 요구 없음 |
| `hosp/transfers` (241만 행) | 병상 이동 화면 없음. ICU 이동 여부는 `icustays`로 판정 가능 |
| `hosp/omr` | 외래 측정치. ED 화면과 무관 |
| `hosp/services` | 진료과 이송 화면 없음 |
| `icu/outputevents` | 배출량 화면 없음 |
| `icu/inputevents` | 승압제 라벨용으로만 유용 → Phase B |
| `icu/procedureevents` | 삽관·심정지 라벨용으로만 유용 → Phase B |
| `ed/medrecon`, `ed/pyxis` | 복용약 항목은 **기록 화면 전용**이며 기록 영역은 이번 범위 밖 → **완전 제외** |
| `hosp/d_icd_procedures` | 시술코드 화면 없음 |

---

## 15. 보안 / 데이터 취급

- 정식 MIMIC-IV 는 **PhysioNet Credentialed Health Data License 1.5.0** 대상입니다. `MIMIC-IV-ED/LICENSE.txt` 참조.
- 현재 적재된 **MIMIC-IV Clinical Database Demo v2.2** 는 **Open Data Commons ODbL v1.0** 대상으로, 자격심사 없이 공개 배포됩니다. 라이선스가 다르다고 해서 취급을 느슨하게 하지 않습니다 — 아래 규칙은 두 데이터셋에 동일하게 적용합니다.
- `.gitignore` 에 `MIMIC-DEMO/` 를 명시했습니다. 하위 `MIMIC-IV-*` 디렉터리는 기존 규칙에도 걸리지만, 최상위에 파일이 놓이는 경우까지 막습니다.
- `.gitignore`가 `*.csv`, `*.csv.gz`, `mimic/`, `data/`, `datasets/`를 이미 제외하고 있음을 확인했습니다. **원천 데이터는 커밋되지 않습니다.**
- 코호트 정의는 `app.cohort` 테이블에 있습니다. stay_id·tier·acuity 등 선별 메타데이터만 담고 **임상값은 포함하지 않습니다**. 저장소에는 파일로 남지 않습니다.
- 문서·로그·스크린샷에 실제 `subject_id`를 노출하지 않습니다.
- `backups/` 의 덤프(`database/scripts/dump_project_tables.sh` 산출물)에는 MIMIC 유래 임상값이 그대로 들어 있습니다. `.gitignore` 대상이며 커밋하지 않습니다. 서버로는 `scp` 로만 전달하고, 적용 후 남겨두지 않습니다 (§12.1).
- `.env`는 커밋하지 않으며, `.env.example`에는 키 이름만 둡니다.
