"""악화(deterioration) 라벨 정의 — 성능 모니터링의 정답 기준.

🔑 왜 이 파일이 필요한가
    `artifacts/bundle.json` 은 **예측에 필요한 것**(feature 순서·lab/vital itemid·운영점)만
    담는다. "예측이 맞았는지" 를 판정하는 라벨 정의는 학습 파이프라인 쪽에 있고
    전달 패키지에서 의도적으로 빠졌다(`_handoff/README_배치안내.md` §7 "미포함 — 학습 코드").
    그래서 라벨 조건만 여기 한 곳에 옮겨 적는다. **이 저장소에서 라벨 정의가 있는 곳은 여기뿐이다.**

🔑 bundle 에 있는 값은 bundle 에서 읽는다
    label_window_h · vital valid_range 는 여기에 적지 않는다. 두 곳에 적으면 어긋난다.

📌 출처 (학습 저장소 `src/config/cohort.yaml` · `src/config/features.yaml`,
    적용 코드 `src/data/build_events.py` · `build_abnormal_events.py` ·
    `build_ed_death.py` · `build_grid_labels.py`)

    y_deterioration = (t, t+3h] 안의
        호흡부전 처치(삽관·침습/비침습 환기) OR 승압제 OR CPR OR 사망(ED+입원)
        OR 생리학적 악화 onset

⚠ 조인키를 섞으면 안 된다.
    처치·사망(admissions)  → hadm_id
    생리학적 악화·ED 사망   → stay_id
  학습 코드 주석: hadm_id 로 붙이면 같은 입원에 묶인 다른 ED 내원에 이벤트가 교차 배정된다.

✅ 검증 (2026-09-04)
    이 정의로 현 코호트의 app.prediction 을 채점하고 학습 산출물
    `cache/grid_labels_C.parquet` 와 (stay_id, t) 로 대조한 결과
    **917행 전부 일치, 양성 27/27 일치**. 나머지 16행은 학습 grid 에 없는 행이며
    (학습 코호트 밖 stay 3 + truncate 구간 13) 내역이 설명된다.
    회귀 테스트: backend/tests/test_model_monitoring.py
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Iterable

# --------------------------------------------------------------------- 처치·사망

# procedureevents. 침습과 비침습을 나누어 두지만 가중치는 같다(niv_weight = 1.0).
RESP_INVASIVE_ITEMIDS = (224385, 225792)   # Intubation · Invasive Ventilation
RESP_NIV_ITEMIDS = (225794,)               # Non-invasive Ventilation
CPR_ITEMIDS = (225466,)                    # Cardiac Arrest

# inputevents 승압제. 강심제(Dobutamine 221653 · Milrinone 221986)는 승압제가 아니라 제외한다.
VASOPRESSOR_ITEMIDS = (
    221906,  # Norepinephrine
    221749,  # Phenylephrine
    229630,
    229632,
    221289,  # Epinephrine
    229617,
    222315,  # Vasopressin
    221662,  # Dopamine
    229709,
    229764,
)

# 주입 에피소드 묶기. 직전까지의 종료시각 + gap 을 넘으면 새 에피소드이고,
# 지속이 min_duration 미만인 에피소드는 이벤트로 세지 않는다(일회성 투여 배제).
VASO_EPISODE_GAP_H = 1.0
VASO_MIN_DURATION_H = 1.0

# emar / prescriptions 로 넘어갈 때 쓰는 약물명 판정. 강심제는 같은 정규식으로 걷어낸다.
VASO_DRUG_PATTERN = re.compile(
    "norepinephrine|levophed|phenylephrine|neo-synephrine|epinephrine|"
    "vasopressin|pitressin|dopamine|dobutamine|milrinone|angiotensin"
)
VASO_INOTROPE_PATTERN = re.compile("dobutamine|milrinone")

# emar 에서 "실제로 투여가 시작됨" 을 뜻하는 event_txt.
VASO_EMAR_START_EVENTS = (
    "Started", "Restarted", "Delayed Started", "Started in Other Location",
    "Administered", "Administered in Other Location", "Delayed Administered",
)

# prescriptions 는 정맥 투여만 이벤트로 본다.
VASO_PRESCRIPTION_ROUTES = ("IV DRIP", "IV", "IV BOLUS")

# 승압제 원천 우선순위. hadm 별로 **가장 높은 순위 하나만** 채택한다(중복 계상 방지).
#   inputevents → emar → prescriptions
VASO_SOURCE_VERSION = "C"

# --------------------------------------------------------------------- 생리학적 악화

# 이상 활력징후 기준. (lo, hi) — 값 < lo 또는 값 >= hi 면 이상이다. None 은 그 방향 기준 없음.
# ⚠ feature 의 abnormal_vital_count 와 기준은 같지만 쓰임이 다르다.
#   feature = 시점 t 의 현재 상태 · label = (t, t+3h] 안의 정상→비정상 **전환(onset)**
#   이미 비정상인 환자에게 "계속 비정상" 을 맞히는 것은 예측이 아니다.
ABNORMAL_VITAL_CRITERIA: dict[str, tuple[float | None, float | None]] = {
    "sbp": (90, None),
    "spo2": (90, None),
    "resp_rate": (8, 30),
    "heart_rate": (40, 130),
    "temperature": (35.5, None),   # ℃
}

# 몇 개 이상 동시에 충족해야 '비정상 상태' 로 보는가.
ABNORMAL_VITAL_MIN_COUNT = 2

# --------------------------------------------------------------------- 식별자

# y_deterioration 에 들어가는 이벤트 종류.
MAIN_EVENT_TYPES = ("resp_inv", "resp_niv", "cpr", "vaso", "death", "abnormal")


def label_definition_id(model_version: str, label_window_h: int) -> str:
    """app.model_outcome.label_definition 에 남길 식별자.

    라벨 정의가 개정되면 이 문자열이 달라지고, 기존 행은 그대로 남는다
    (UNIQUE 에 label_definition 이 들어 있다). 과거 성능의 근거를 덮어쓰지 않기 위함이다.
    """
    return f"training_{model_version}_labelB_src{VASO_SOURCE_VERSION}_win{label_window_h}h"


# --------------------------------------------------------------------- 판정 로직


class Events:
    """악화 이벤트 목록. 조인키가 다르므로 두 갈래로 나눠 담는다.

    by_hadm  처치(호흡부전·CPR·승압제)와 입원 중 사망
    by_stay  생리학적 악화 onset 과 ED 사망
    """

    __slots__ = ("by_hadm", "by_stay")

    def __init__(self) -> None:
        self.by_hadm: dict[int, list[tuple[datetime, str]]] = {}
        self.by_stay: dict[int, list[tuple[datetime, str]]] = {}

    def _add(self, bucket: dict[int, list[tuple[datetime, str]]],
             key: int | None, moment: datetime | None, kind: str) -> None:
        if key is None or moment is None:
            return
        bucket.setdefault(int(key), []).append((moment, kind))

    def add_hadm(self, hadm_id: int | None, moment: datetime | None, kind: str) -> None:
        self._add(self.by_hadm, hadm_id, moment, kind)

    def add_stay(self, stay_id: int | None, moment: datetime | None, kind: str) -> None:
        self._add(self.by_stay, stay_id, moment, kind)

    def within(self, stay_id: int, hadm_id: int | None,
               start: datetime, end: datetime) -> list[tuple[datetime, str]]:
        """(start, end] 안의 이벤트. 좌개·우폐 구간이다."""
        found = [e for e in self.by_hadm.get(hadm_id, ()) if start < e[0] <= end] if hadm_id else []
        found += [e for e in self.by_stay.get(stay_id, ()) if start < e[0] <= end]
        return sorted(found)

    def first_event(self, stay_id: int, hadm_id: int | None) -> datetime | None:
        """그 환자의 첫 악화 시각. 학습 grid 가 여기서 끊긴다."""
        moments = [e[0] for e in self.by_hadm.get(hadm_id, ())] if hadm_id else []
        moments += [e[0] for e in self.by_stay.get(stay_id, ())]
        return min(moments) if moments else None


def vasopressor_episode_starts(rows: Iterable[Any]) -> list[tuple[int, datetime]]:
    """승압제 주입행을 에피소드로 묶어 시작 시각만 남긴다.

    직전까지의 종료시각 누적 최대값 + gap 을 넘으면 새 에피소드다.
    지속이 min_duration 미만인 에피소드는 이벤트로 세지 않는다(일회성 투여 배제).

    ⚠ 입력은 (icu_stay_id, itemid, starttime) 순으로 정렬돼 있어야 한다.
    """
    gap = timedelta(hours=VASO_EPISODE_GAP_H)
    starts: list[tuple[int, datetime]] = []
    key: tuple[Any, Any] | None = None
    prev_end: datetime | None = None
    ep_start: datetime | None = None
    ep_end: datetime | None = None
    hadm: int | None = None

    def flush() -> None:
        if ep_start is not None and hadm is not None:
            if (ep_end - ep_start) >= timedelta(hours=VASO_MIN_DURATION_H):
                starts.append((hadm, ep_start))

    for row in rows:
        row_key = (row["icu_stay_id"], row["itemid"])
        start, end = row["starttime"], row["endtime"]
        if row_key != key:
            flush()
            key, prev_end = row_key, None
            ep_start, ep_end, hadm = None, None, None
        if prev_end is None or start > prev_end + gap:
            flush()
            ep_start, ep_end = start, end
        else:
            ep_end = max(ep_end, end)
        hadm = int(row["hadm_id"])
        prev_end = end if prev_end is None else max(prev_end, end)
    flush()
    return starts


def _matches_vasopressor(name: str | None) -> bool:
    """약물명이 승압제인가. 강심제는 같은 목록에서 걷어낸다."""
    if not name:
        return False
    lowered = name.lower()
    return bool(VASO_DRUG_PATTERN.search(lowered)) and not VASO_INOTROPE_PATTERN.search(lowered)


def vasopressor_events(
    *,
    infusion_rows: Iterable[Any],
    emar_rows: Iterable[Any],
    prescription_rows: Iterable[Any],
) -> list[tuple[int, datetime]]:
    """승압제 이벤트 — 3단 우선순위(source_version=C).

    hadm 별로 **가장 높은 순위의 원천 하나만** 채택한다. 섞으면 같은 투여가
    두 번 세어져 양성이 부풀려진다.
        inputevents(주입 에피소드) → emar(실제 투여) → prescriptions(처방)
    """
    infusions: dict[int, list[datetime]] = {}
    for hadm, moment in vasopressor_episode_starts(infusion_rows):
        infusions.setdefault(hadm, []).append(moment)

    emar: dict[int, list[datetime]] = {}
    for row in emar_rows:
        if row["event_txt"] in VASO_EMAR_START_EVENTS and _matches_vasopressor(row["medication"]):
            emar.setdefault(int(row["hadm_id"]), []).append(row["charttime"])

    orders: dict[int, list[datetime]] = {}
    for row in prescription_rows:
        if row["route"] in VASO_PRESCRIPTION_ROUTES and _matches_vasopressor(row["drug"]):
            orders.setdefault(int(row["hadm_id"]), []).append(row["starttime"])

    events: list[tuple[int, datetime]] = []
    for hadm in set(infusions) | set(emar) | set(orders):
        chosen = infusions.get(hadm) or emar.get(hadm) or orders.get(hadm) or []
        events.extend((hadm, moment) for moment in chosen)
    return events


# ED vitalsign 컬럼 → 라벨 기준 변수명. 기준에 쓰는 변수만 본다(학습 코드도 CRIT 로 거른다).
# dbp·mbp·GCS 는 이상 기준에 없으므로 읽지 않는다.
_ED_VITAL_COLUMNS = {
    "heartrate": "heart_rate",
    "resprate": "resp_rate",
    "o2sat": "spo2",
    "sbp": "sbp",
    "temperature": "temperature",   # ℉ → ℃ 변환 대상
}


def _f_to_c(fahrenheit: float) -> float:
    """℉ → ℃. 반올림하지 않는다 — 배치가 반올림하지 않기 때문이다."""
    return (fahrenheit - 32) * 5 / 9


def chart_itemid_map(vital_itemids: dict[str, list[int]]) -> dict[int, tuple[str, bool]]:
    """bundle.vital_itemids → {itemid: (변수명, ℉→℃ 필요 여부)}. 이상 기준 변수만 남긴다.

    feature 쪽 대응물은 `repositories/ml_features._chartevents_itemids()` 다.
    쓰임이 달라(저기는 feature 100개용, 여기는 라벨 기준 5종용) 따로 둔다.
    """
    out: dict[int, tuple[str, bool]] = {}
    for key, itemids in vital_itemids.items():
        if key.endswith("_f"):
            var, needs_convert = key[:-2], True
        elif key.endswith("_c"):
            var, needs_convert = key[:-2], False
        else:
            var, needs_convert = key, False
        if var not in ABNORMAL_VITAL_CRITERIA:
            continue
        for itemid in itemids:
            out[int(itemid)] = (var, needs_convert)
    return out


def _in_range(var: str, value: float, valid_range: dict[str, list[float]]) -> bool:
    """생리학적으로 불가능한 값은 측정 자체를 없던 것으로 본다(배치 _clip 과 같다)."""
    bounds = valid_range.get(var)
    if not bounds:
        return True
    return bounds[0] <= value <= bounds[1]


def abnormal_onsets(
    *,
    ed_vital_rows: Iterable[Any],
    chart_vital_rows: Iterable[Any],
    stays: dict[int, dict[str, Any]],
    hadm_to_stays: dict[int, list[int]],
    itemid_map: dict[int, tuple[str, bool]],
    valid_range: dict[str, list[float]],
) -> list[tuple[int, datetime]]:
    """생리학적 악화 onset — (stay_id, 시각).

    관찰창 [ed_intime, obs_end] 안의 측정만 쓰고, 측정 시각마다 LOCF 로 마지막 값을 채운 뒤
    이상 기준을 min_count 개 이상 충족하는지 본다. **정상(또는 관측 없음) → 비정상**으로
    바뀌는 순간만 이벤트다.

    ⚠ 이미 비정상인 상태가 계속되는 것은 이벤트가 아니다. 그것을 맞히는 것은 예측이 아니다.
    """
    measures: dict[int, list[tuple[datetime, str, float]]] = {}

    def keep(stay_id: int, moment: datetime, var: str, value: float) -> None:
        stay = stays.get(stay_id)
        if stay is None or moment is None:
            return
        if not (stay["ed_intime"] <= moment <= stay["obs_end"]):
            return
        if not _in_range(var, value, valid_range):
            return
        measures.setdefault(stay_id, []).append((moment, var, value))

    for row in ed_vital_rows:
        stay_id = int(row["stay_id"])
        for column, var in _ED_VITAL_COLUMNS.items():
            raw = row[column]
            if raw is None:
                continue
            value = _f_to_c(float(raw)) if var == "temperature" else float(raw)
            keep(stay_id, row["charttime"], var, value)

    for row in chart_vital_rows:
        mapped = itemid_map.get(int(row["itemid"]))
        if mapped is None:
            continue
        var, needs_convert = mapped
        value = float(row["valuenum"])
        if needs_convert:
            value = _f_to_c(value)
        for stay_id in hadm_to_stays.get(int(row["hadm_id"]), ()):
            keep(stay_id, row["charttime"], var, value)

    onsets: list[tuple[int, datetime]] = []
    for stay_id, rows in measures.items():
        # 같은 시각의 여러 측정은 한 관측으로 묶는다(배치의 pivot aggregate=last 와 같다).
        at_time: dict[datetime, dict[str, float]] = {}
        for moment, var, value in rows:
            at_time.setdefault(moment, {})[var] = value

        carried: dict[str, float] = {}
        previously_abnormal = False
        for moment in sorted(at_time):
            carried.update(at_time[moment])          # LOCF
            hits = 0
            for var, (low, high) in ABNORMAL_VITAL_CRITERIA.items():
                value = carried.get(var)
                if value is None:
                    continue
                if (low is not None and value < low) or (high is not None and value >= high):
                    hits += 1
            abnormal = hits >= ABNORMAL_VITAL_MIN_COUNT
            if abnormal and not previously_abnormal:
                onsets.append((stay_id, moment))
            previously_abnormal = abnormal
    return onsets


def _procedure_event_type(itemid: int) -> str | None:
    if itemid in RESP_INVASIVE_ITEMIDS:
        return "resp_inv"
    if itemid in RESP_NIV_ITEMIDS:
        return "resp_niv"
    if itemid in CPR_ITEMIDS:
        return "cpr"
    return None


def build_events(
    *,
    procedure_rows: Iterable[Any],
    vasopressor_starts: Iterable[tuple[int, datetime]],
    hospital_death_rows: Iterable[Any],
    ed_death_rows: Iterable[Any],
    onsets: Iterable[tuple[int, datetime]],
) -> Events:
    """원천 행들을 판정용 이벤트 목록으로 모은다."""
    events = Events()

    for row in procedure_rows:
        kind = _procedure_event_type(int(row["itemid"]))
        if kind:
            events.add_hadm(row["hadm_id"], row["starttime"], kind)

    for hadm_id, moment in vasopressor_starts:
        events.add_hadm(hadm_id, moment, "vaso")

    for row in hospital_death_rows:
        events.add_hadm(row["hadm_id"], row["deathtime"], "death")

    # ED 사망은 stay_id 키다. 대상 대부분이 hadm_id 를 갖지 않아 입원 기록으로는 못 잡는다.
    for row in ed_death_rows:
        events.add_stay(row["stay_id"], row["outtime"], "death")

    for stay_id, moment in onsets:
        events.add_stay(stay_id, moment, "abnormal")

    return events


def evaluate_prediction(
    *,
    events: Events,
    stay_id: int,
    hadm_id: int | None,
    prediction_time: datetime,
    window_h: int,
) -> tuple[int, str | None, datetime | None]:
    """예측 1건을 채점한다 → (라벨, 이벤트 종류, 이벤트 시각).

    구간은 (t, t+window] 로 좌개·우폐다. 여러 이벤트가 걸리면 **가장 이른 것**을 남기고,
    같은 시각이면 MAIN_EVENT_TYPES 순서로 정한다(표시용 대표값일 뿐 라벨은 같다).
    """
    found = events.within(stay_id, hadm_id, prediction_time,
                          prediction_time + timedelta(hours=window_h))
    if not found:
        return 0, None, None
    moment, kind = min(found, key=lambda e: (e[0], MAIN_EVENT_TYPES.index(e[1])))
    return 1, kind, moment


def is_after_first_event(
    *,
    events: Events,
    stay_id: int,
    hadm_id: int | None,
    prediction_time: datetime,
) -> bool:
    """학습 grid 밖(첫 악화 이후) 시점인가.

    학습은 grid_end = min(obs_end, 첫 악화) 에서 끊으므로 첫 악화 **이후** 시점은
    학습 분포에 없다. 지표에서 빼고 표시만 한다.
    (경계 포함 여부는 학습 산출물과 대조해 확인했다 — t == 첫 악화 는 grid 에 남는다.)
    """
    first = events.first_event(stay_id, hadm_id)
    return first is not None and prediction_time > first
