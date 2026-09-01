# 서빙 패키지


> 🔑 **모델 C (production refit · 2008-2019 전체)** · threshold `0.035838` · v2.0.0
> **C 는 직접 측정한 test 성능이 없다.** 아래 수치는 동일 고정 사양의 temporal holdout
> (B pristine, 2008–2013 → 2017–2019 1회) 결과이며 **C artifact 자체를 잰 값이 아니다.**
> 🚨 금지: "C TEST PR-AUC = …" · "A 대비 향상" · "완전한 blind test"
> 확정 2026-08-31 · 온라인/배치 일치 검증 통과 (위험도 오차 **0.00e+00**)
> 성능은 **Pristine temporal 평가** 기준 — 아래 「성능」 절의 【B】를 인용할 것

ED 내원 성인 환자의 악화 확률을 반환한다. 이 디렉토리만으로 자기완결적으로 동작한다.

```
예측 대상   성인(18세 이상) ED 내원 환자
예측 시점   ED 도착 +1h 부터 1시간 간격, ED 퇴실 +2h 까지
반환        (t, t+3h] 안에 악화할 보정 확률 (0~1)
악화 정의   호흡부전 처치(삽관·환기) OR 승압제 OR CPR OR 사망(ED+입원) OR 생리학적 악화
```

## 🔒 배포 모델 확정 (2026-08-31)

**모델 A(`artifacts/`)를 최종 배포 모델로 LOCK 한다.**

| 항목 | 값 |
|---|---|
| threshold | **0.035838** |
| feature | 100개 |
| model_lgbm.pkl | seed 3개 앙상블 · hash 불변 |

Pristine 검증 과정에서 확인된 serving observability 개선사항인 **missing feature NULL 보완
logging** 을 최종 배포 serving 코드에도 반영하였다. 이 변경은 **model artifact,
feature specification, prediction logic 및 threshold 를 변경하지 않는다.**

⚠ **Pristine 평가 모델(B)의 threshold `0.035838` 를 A 에 적용하지 않는다.**
B 는 배포 모델이 아니며, 사양의 시간적 일반화 성능을 재기 위한 별도 평가 모델이다.


## 구성

| 파일 | 역할 |
|---|---|
| `service.py` | FastAPI 래퍼 (`/health` · `/info` · `/predict`) — **이 저장소에서 작성** |
| `predict_service_with_reason_v3.py` | `RiskService` — 모델 로드·예측·운영점·위험구간·설명 |
| `reason_engine_v3.py` | 기여 신호 문장화 + **임상 방향 gate** (`pred_contrib` → "심박수 122 bpm") |
| `online_features.py` | `OnlineFeatureBuilder` — 원본 관측 → feature 100개 |
| `calibrators.py` | Platt 보정기 (**파일명 변경 금지** — pickle 이 참조) |
| `tests/test_online_parity.py` | 온라인 ↔ 배치 일치 검증 |
| `requirements.txt` | 의존성 (**버전 고정**. 컨테이너가 이 파일만 설치한다) |

⚠ **파일명을 바꾸지 마라.** 모델 담당자 전달본 이름 그대로 둔다. `service.py` 와
  `tests/test_online_parity.py` 가 `predict_service_with_reason_v3` 를 import 한다.

⚠ **전달본에 딸려 오는 부속 파일은 쓰지 않는다.**
  - `reason_engine_v2.py` · `predict_service_with_reason_v2.py` — v3 로 넘겨주는 shim 이다.
    이 저장소에는 v2 를 import 하는 코드가 없어 삭제했다(있으면 오히려 엔진이 둘로 보인다).
  - `requirements_v3.txt` — `requirements.txt` 의 부분집합인데 버전이 고정돼 있지 않다.
    그대로 설치하면 scikit-learn 메이저가 올라가 `calibrator_platt.pkl` 언피클이 깨진다.

🔧 **전달본에 매번 다시 넣어야 하는 호환 패치가 있다** (`# [ER:ON compat]` 으로 표시).
  배포 artifact 는 아직 구 feature naming 이라, 없으면 화면에 `dbp min 71.4` 같은 raw
  feature 명이 나오고 `*_dt`(시간)를 분으로 읽어 3.2시간이 "3분"으로 표시된다.
  다음 전달본에 반영해 달라고 모델 담당자에게 요청할 것.

필요한 아티팩트 5개: `bundle.json` · `model_lgbm.pkl`(34MB) · `calibrator_platt.pkl` ·
`feature_spec.json` · `text_transformer.pkl`

이 중 `.pkl` 3개는 저장소에 커밋하지 않는다(`.gitignore`). 모델 담당자에게 따로 받아
`artifacts/` 에 넣은 뒤, LOCK 된 그 전달본이 맞는지 대조한다.

```bash
cd artifacts && shasum -a 256 -c CHECKSUMS.txt
```

⚠ **하나라도 `FAILED` 가 나오면 그 전달본을 쓰지 마라.** 버전이 다른 artifact 를 섞으면
  예측값이 조용히 달라진다.

`artifacts/CHECKSUMS.txt` 는 새 전달본을 LOCK 할 때만 갱신한다.

```bash
cd artifacts && shasum -a 256 *.json *.pkl > CHECKSUMS.txt
```

## 설치

```bash
pip install -r requirements.txt
```

## 사용

```python
from online_features import OnlineFeatureBuilder
from predict_service_with_reason_v3 import RiskService
import polars as pl

fb  = OnlineFeatureBuilder(art_dir="artifacts")
svc = RiskService(art_dir="artifacts")

patient = dict(stay_id=123, age=67, ed_intime=t0, chiefcomplaint="chest pain",
               triage_temperature=37.2, triage_heartrate=104, triage_resprate=22,
               triage_o2sat=94, triage_sbp=98, triage_dbp=61,
               triage_pain=7, triage_acuity=2)
vitals = [(ts, "heart_rate", 104.0), (ts, "sbp", 98.0), ...]   # charttime <= t
labs   = [(ts, "lactate", 3.1), ...]                            # storetime <= t

row = fb.build(patient, vitals, labs, t)          # 한 시점
out = svc.score_rows(pl.DataFrame([row]))
# out: risk(0~1) · band(red/amber/green) · label · alert
```

여러 시점은 `fb.build_series(patient, vitals, labs, t_start, n_steps)` 를 쓴다.
`t_start` 는 **ED 도착 + 1h** 다.

## 설명(reason)

`/predict` 는 시점마다 기여 신호를 함께 돌려준다.

| 필드 | 내용 |
|---|---|
| `reason` | 화면에 그대로 쓰는 문장 (` · ` 로 이어 붙인 최대 2개) |
| `reason_detail[]` | `{feature, feature_label, text, value \| previous_value·current_value, contribution \| delta_contribution, contribution_space}` |
| `reason_type` | `risk_increase_signal`(직전 대비 상승 기여) · `current_risk_signal`(현재 위험 기여) |
| `reason_title` · `reason_basis` | 화면 제목과 산출 근거 |
| `risk_delta` · `risk_delta_pct_point` | 직전 시점 대비 **보정 확률** 변화. 첫 시점은 `null` |
| `reason_notice` | 설명과 **반드시 함께** 표시할 문구 (응답 최상위 1개) |

⚠ `contribution` 계열의 단위는 `lightgbm_raw_score_shap` 이다. **확률 %p 가 아니다.**

🔴 이것은 **악화의 원인이 아니라** 모델 예측에 기여한 신호다. 화면 문구를
"원인/요인" 으로 쓰지 말 것. 설명 생성이 실패해도 위험도는 그대로 반환된다
(`reason` 계열만 `null` · 서비스 로그에 경고).

### 임상 방향 gate (v3.1)

위험 상승 시점의 악화 신호는 **fail-closed** 로 거른다. Δcontribution 이 양수여도
아래를 모두 만족해야 화면에 나온다.

1. 그 feature 값이 실제로 변했다
2. 정적·latent·workflow proxy(측정 횟수, 경과시간, mask/missing)가 아니다
3. 사전 정의된 규칙에서 **worsening** 으로 판정됐다 (vital severity · lab 방향 · 치료 강도)

방향 규칙이 없는 feature 는 `unknown` 으로 제외된다. 그래서 다음 상태가 **정상**으로 존재한다.

| reason_type | 뜻 | reason_detail |
|---|---|---|
| `risk_increase_clinical_worsening_signal` | 위험 상승 + 악화로 확인된 변화 | 있음 |
| `risk_increase_without_confirmed_clinical_worsening_signal` | 위험은 올랐지만 확인된 악화 변화 없음 | **빈 목록이 정상** |
| `current_risk_signal` | 현재 위험도 기여 신호 | 있음 |

⚠ 두 번째 상태를 "설명 없음/모델 미제공" 으로 표시하면 안 된다. gate 결과를 그대로 알려야 한다.
   현재 위험 신호(`build_reason`)에는 gate 가 적용되지 않는다 — `hours_from_ed` 같은
   비임상 proxy 가 그대로 올라올 수 있다.

## 화면 표시 규칙

| 구간 | 경계 | 표시 | 실측 악화율 |
|---|---|---|---:|
| 🔴 | ≥ 13.71% | **매우 악화** | 51.4% (약 2명 중 1명) |
| 🟡 | 4.03% ~ 13.71% | **악화** | 10.7% (약 9명 중 1명) |
| 🟢 | < 4.03% | **저위험** | 0.4% (약 241명 중 1명) |

**반드시 지킬 것**

1. **보정 확률을 그대로 0~100% 로 표시**한다. 구간 평균으로 바꾸지 마라.
2. **"5시간 내"가 아니라 "3시간 내"** 다.
3. 🟢 는 **"정상"이 아니라 "저위험"** — 실제 악화 환자가 1,447명(양성의 15.0%) 있다.
4. 자연빈도("약 N명 중 1명")는 **개별 확률 기준**으로 계산한다. 구간 평균을 쓰면
   "96.2% 인데 2명 중 1명" 같은 모순이 생긴다.

## 운영점

기본 `recall_85` (threshold 0.0358). `bundle.json["operating_points"]` 에 5종이 있다.

| 운영점 | Recall | Precision | 경보율 |
|---|---:|---:|---:|
| recall_70 | 0.700 | 0.606 | 2.7% |
| recall_80 | 0.800 | 0.430 | 4.3% |
| **recall_85** ← 기본 | 0.850 | 0.333 | 5.9% |
| recall_90 | 0.900 | 0.248 | 8.5% |
| recall_95 | 0.951 | 0.123 | 17.9% |

`svc.set_operating_point("recall_90")` 으로 바꿀 수 있다.
⚠ 바꾸면 위험구간 경계도 함께 재검토해야 한다.

## 🔴 반드시 지킬 것

| 항목 | 내용 |
|---|---|
| 시간 규칙 | vital `charttime <= t` · lab **`storetime <= t`** · triage 제한 없음 |
| 단위 | `triage_temperature` 는 **℃** (MIMIC 원본 ℉ → `(°F-32)*5/9`) |
| feature 순서 | `bundle.json["features"]` 를 그대로 쓴다. 하드코딩 금지 |
| 변환기 | `text_transformer.pkl` 을 **재학습하지 마라**. 로드해서 `transform` 만 |
| 적용 범위 | ED 도착 +1h ~ ED 퇴실 +2h. 밖은 `null` |
| 일치 검증 | `python test_online_parity.py` 를 **정기 실행**. 위험도 오차 0 이어야 한다 |

## 성능

🔑 **두 가지가 있고 섞으면 안 된다.**

### 【A】 이 아티팩트가 실제로 학습된 조건 — ⚠ 오염된 평가

무작위 70% split 으로 학습(297,174 stay), 무작위 test 로 평가. 그 TEST 는 feature selection·
threshold·window·cohort 선택에 반복 사용된 뒤 설계가 변경됐다 → **성능 근거로 쓰지 말 것.**

```
PR-AUC 0.6427 · AUROC 0.9602 · Recall 0.8290
Precision 0.3150 · F1 0.4566 · threshold 0.035838
```

### 【B】 같은 사양의 Pristine temporal 평가 — **인용할 성능**

2008–2013 학습 → 2014–2016 으로 보정·threshold → **2017–2019 를 1회 평가**.

| 지표 | 값 | 95% CI | 목표 |
|---|---:|---:|---:|
| PR-AUC | 0.6644 | [0.6427, 0.6854] | ≥0.55 ✅ |
| AUROC | 0.9636 | [0.9594, 0.9676] | ≥0.93 ✅ |
| Recall | 0.8278 | — | ≥0.90 ❌ |
| Precision | 0.3741 | — | ≥0.30 ✅ |
| F1 | 0.5153 | — | ≥0.40 ✅ |

⚠ **【B】는 이 아티팩트가 아니라 같은 사양을 과거 데이터로 학습한 별도 모델이다.**
배포 모델의 성능을 직접 잰 것이 아니라, **이 사양이 미래 데이터에서 내는 성능**의
편향 없는 추정치다. 상세: `final_evaluation/evaluation_report.md`

🚨 두 수치를 "개선"으로 비교하지 말 것 — 평가 모집단 양성률이 2.28% vs 2.89% 로 다르고
`F1@고정Recall` 은 prevalence 의 함수다.

## 한계

- 학습 코호트는 **MIMIC-IV 성인 ED 내원 전체**다. 악화율이 다른 기관에서는 위험구간 경계와
  F1 이 그대로 옮겨가지 않는다 — `F1@고정Recall` 은 모델 × 양성률의 함수다.
- `icu/chartevents` 커버리지 4.9% — ED 퇴실 후 활력징후가 거의 갱신되지 않는다.
- `_dt`(측정 빈도) 계열이 상위 gain — 진료 패턴이 다른 기관에서 약해질 수 있다.
