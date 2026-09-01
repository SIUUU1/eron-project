"""ER:ON explanation engine v3.

핵심 변경점 (v3 FINAL)
----------------------
1. 현재 위험 신호와 직전 대비 위험 상승 신호 모두 "실제 model feature" 단위로 설명한다.
2. 직전 대비 상승 신호는 feature들을 vital/lab 그룹으로 먼저 합치지 않는다.
3. delta contribution이 발생한 정확한 feature의 이전값/현재값만 화면에 표시한다.
4. 값이 실제로 변하지 않은 feature는 change reason에서 제외한다.
5. **clinical-direction gate**를 추가한다.
   - model Δcontribution > 0
   - exact feature 값 변화 존재
   - 사전 정의된 임상 규칙상 worsening 방향
   위 3가지를 모두 만족하는 경우에만 사용자 화면의 "악화 신호"로 노출한다.
6. 개선(improving), 중립(neutral), 방향 미정(unknown) 변화는 악화 신호에서 제외한다.
7. 측정 횟수/missingness/시간 간격 같은 workflow/proxy feature는 사용자 악화 신호에서 제외한다.
8. 결과에 feature명, 이전/현재 contribution, delta contribution, 실제 값, 임상 방향/규칙을 함께 남긴다.

중요
----
LightGBM ``pred_contrib=True`` 값은 raw model score 공간의 SHAP contribution이다.
따라서 ``delta_contribution``의 단위는 calibrated probability의 %p가 아니다.
이 모듈은 모델 설명을 제공할 뿐 임상적 인과관계를 주장하지 않는다.
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np


# =============================================================================
# 1. Display labels
# =============================================================================

LAB_LABELS = {
    "albumin": "Albumin",
    "alp": "ALP",
    "alt": "ALT",
    "anion_gap": "Anion gap",
    "ast": "AST",
    "base_excess": "Base excess",
    "bicarbonate": "Bicarbonate",
    "bilirubin_total": "Total bilirubin",
    "bun": "BUN",
    "calcium_total": "Calcium",
    "chloride": "Chloride",
    "ck": "CK",
    "creatinine": "Creatinine",
    "crp": "CRP",
    "egfr": "eGFR",
    "free_calcium": "Ionized calcium",
    "glucose": "Glucose",
    "hematocrit": "Hematocrit",
    "hemoglobin": "Hemoglobin",
    "inr": "INR",
    "lactate": "Lactate",
    "ldh": "LDH",
    "lymphocytes": "Lymphocytes",
    "magnesium": "Magnesium",
    "mcv": "MCV",
    "neutrophils": "Neutrophils",
    "ntprobnp": "NT-proBNP",
    "pco2": "pCO₂",
    "ph": "pH",
    "phosphate": "Phosphate",
    "platelet": "Platelet",
    "po2": "pO₂",
    "potassium": "Potassium",
    "pt": "PT",
    "ptt": "PTT",
    "rbc": "RBC",
    "rdw": "RDW",
    "sodium": "Sodium",
    "troponin_t": "Troponin T",
    "wbc": "WBC",
}

# 실제 ER:ON feature schema는 hr_*, rr_*, spo2_*, sbp_* 형태다.
# 과거 코드 호환을 위해 heart_rate_*, resp_rate_*도 같이 허용한다.
VITAL_PREFIXES = {
    "heart_rate": ("심박수", "hr"),
    "resp_rate": ("호흡수", "rr"),
    "spo2": ("산소포화도", "spo2"),
    "sbp": ("수축기 혈압", "sbp"),
    "hr": ("심박수", "hr"),
    "rr": ("호흡수", "rr"),
    # [ER:ON compat] 배포 artifact(bundle.json)에 dbp_*, temperature_*, mbp_* 가 있다.
    # 없으면 화면에 "dbp min 71.4" 처럼 raw feature 명이 그대로 나온다.
    # ⚠ 임상 방향 gate 에는 영향이 없다 — dbp/temperature 계열은 severity 규칙이 없어
    #   여전히 unknown(fail-closed)으로 악화 신호에서 제외된다.
    "temperature": ("체온", "temperature"),
    "dbp": ("이완기 혈압", "dbp"),
    "mbp": ("평균동맥압", "dbp"),
}

TRIAGE_LABELS = {
    "acuity": "초기 Triage Acuity",
    "dbp": "초기 이완기 혈압",
    "hr": "초기 심박수",
    "map": "초기 평균동맥압",
    "pain": "초기 통증점수",
    "pulse_pressure": "초기 맥압",
    "rr": "초기 호흡수",
    "sbp": "초기 수축기 혈압",
    "shock_index": "초기 Shock Index",
    "spo2": "초기 산소포화도",
    "temperature_c": "초기 체온",
    # [ER:ON compat] 배포 artifact 의 triage feature 명
    "heartrate": "초기 심박수",
    "resprate": "초기 호흡수",
    "o2sat": "초기 산소포화도",
    "temperature": "초기 체온",
}

CC_KEYWORD_LABELS = {
    "abdominal": "복통",
    "ams": "의식변화",
    "bleeding": "출혈",
    "cardiac": "심장 관련",
    "chestpain": "흉통",
    "dyspnea": "호흡곤란",
    "hypotension": "저혈압",
    "neuro": "신경학적 증상",
    "overdose": "과량복용",
    "sepsis": "패혈증",
    "syncope": "실신",
    "trauma": "외상",
}

DERIVED_LABELS = {
    "abn_hr": "심박수 중증 이상 여부",
    "abn_rr": "호흡수 중증 이상 여부",
    "abn_spo2": "산소포화도 중증 이상 여부",
    "abn_sbp": "수축기 혈압 중증 이상 여부",
    "abnormal_vital_count": "비정상 활력징후 수",
    "abnormal_vital_n_unknown": "미측정 활력징후 수",
    # [ER:ON compat] 배포 artifact 의 파생 feature 명
    "abnormal_vital_persistence": "비정상 활력징후 지속 시점 수",
    "abnormal_vital_delta": "비정상 활력징후 수 변화량",
    "abnormal_vital_slope": "비정상 활력징후 수 변화율",
    "mod_shock_index": "Modified Shock Index",
    "map_est": "평균동맥압(추정)",
    "multi_system_abnormal_count": "다계통 활력징후 이상 수",
    "shock_index": "Shock Index",
    "modified_shock_index": "Modified Shock Index",
    "shock_pattern": "Shock pattern",
    "respiratory_failure_pattern": "호흡부전 패턴",
    "silent_hypoxemia_pattern": "Silent hypoxemia pattern",
    "pulse_pressure": "맥압",
    "map_last": "평균동맥압",
    "map_derived_or_measured": "평균동맥압",
    "dbp_last": "이완기 혈압",
    "temperature_last": "체온",
    "nlr": "NLR",
    "bun_creatinine_ratio": "BUN/Creatinine ratio",
    "ast_alt_ratio": "AST/ALT ratio",
}

BINARY_FEATURES = {
    "abn_hr",
    "abn_rr",
    "abn_spo2",
    "abn_sbp",
    "abn_temperature",
    "shock_pattern",
    "respiratory_failure_pattern",
    "silent_hypoxemia_pattern",
    "prior_ed_within_30d",
    "prior_ed_within_365d",
    "no_prior_ed_visit",
    "arrival_is_ambulance",
    "arrival_is_night",
    "arrival_is_weekend",
    "cc_missing",
    "age_missing",
    "gender_missing",
    "arrival_transport_missing",
}

# 시간별 변화 설명에서 제외할 정적 feature.
STATIC_PREFIXES = (
    "triage_",
    "cc_svd_",
    "cc_kw_",
    "gender__",
    "arrival_transport__",
)

STATIC_EXACT = {
    "age",
    "age_missing",
    "gender_missing",
    "arrival_hour",
    "arrival_dow",
    "arrival_is_ambulance",
    "arrival_is_night",
    "arrival_is_weekend",
    "arrival_transport_missing",
    "prior_ed_visit_count",
    "hours_since_prior_ed",
    "prior_ed_within_30d",
    "prior_ed_within_365d",
    "no_prior_ed_visit",
    "cc_missing",
    "cc_len",
}


# =============================================================================
# 1-1. Clinical-direction gate configuration
# =============================================================================

# 사용자 화면의 "악화 신호"는 fail-closed 방식으로 동작한다.
# 즉, 아래에서 임상 방향을 명시적으로 판정할 수 없는 feature는 unknown으로 처리하고
# 사용자 악화 신호에서 제외한다. 모델 attribution 자체는 debug payload에서 확인 가능하다.
CLINICAL_GATE_VERSION = "v3.1_final_clinical_direction_gate_2"

CLINICAL_WORSENING = "worsening"
CLINICAL_IMPROVING = "improving"
CLINICAL_NEUTRAL = "neutral"
CLINICAL_UNKNOWN = "unknown"

# 측정 행동/결측/시간 경과 등은 모델에 유용할 수 있지만 생리학적 악화라고 직접 표현하지 않는다.
NON_CLINICAL_PROXY_PATTERNS = (
    "_n_measure_",
    "_delta_t_min",
    "time_since_",
    "_mask",
    "_missing",
    "missing_",
    "hours_from_ed",
    "hours_since_ed_arrival",
)

# 치료 강도 증가를 중증도 상승 신호로 해석할 수 있는 feature 이름 토큰.
# 0→1 또는 dose 상승은 worsening, 1→0 또는 dose 감소는 improving으로 판정한다.
# 단, time_since/off_duration 류는 치료 강도 자체가 아니므로 아래 helper에서 제외한다.
TREATMENT_ESCALATION_TOKENS = (
    "vasopressor",
    "pressor",
    "norepinephrine",
    "noradrenaline",
    "epinephrine",
    "adrenaline",
    "vasopressin",
    "phenylephrine",
    "dopamine",
    "dobutamine",
    "mechanical_ventilation",
    "mechanical_ventilator",
    "ventilator",
    "intubation",
    "intubated",
    "invasive_ventilation",
    "crrt",
    "ecmo",
)

# 단위 의존성이 비교적 낮고 방향성이 명확한 lab만 사용자 악화 gate에 사용한다.
# U-shaped 위험(예: Na/K/glucose/pH/WBC)은 단순 증가/감소 규칙으로 오판할 수 있어 제외한다.
LAB_HIGHER_WORSE = {
    "alp",
    "alt",
    "anion_gap",
    "ast",
    "bilirubin_total",
    "bun",
    "ck",
    "creatinine",
    "crp",
    "inr",
    "lactate",
    "ldh",
    "neutrophils",
    "ntprobnp",
    "pt",
    "ptt",
    "rdw",
    "troponin_t",
}

LAB_LOWER_WORSE = {
    "albumin",
    "bicarbonate",
    "egfr",
    "hematocrit",
    "hemoglobin",
    "lymphocytes",
    "platelet",
    "po2",
    "rbc",
}

# lab suffix 중 값의 임상 방향을 비교해도 의미가 비교적 명확한 항목.
LAB_DIRECTIONAL_SUFFIXES = {
    "value",
    "last",
    "first_24h",
    "delta_24h",
    "change_24h",
    "change_rate_per_h",
}


# =============================================================================
# 2. Basic helpers
# =============================================================================


def _value(row: dict[str, Any], key: str) -> float | None:
    """숫자값은 float로 변환하고 NULL/NaN/inf는 None으로 반환한다."""
    v = row.get(key)
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(x):
        return None
    return x


def _same_value(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return bool(np.isclose(a, b, rtol=1e-8, atol=1e-10))


def _fmt_num(v: float) -> str:
    a = abs(v)
    if a >= 1000:
        return f"{v:,.0f}"
    if a >= 100:
        return f"{v:.0f}"
    if a >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _fmt_minutes(v: float) -> str:
    if v < 60:
        return f"{v:.0f}분"
    if v < 60 * 48:
        return f"{v / 60:.1f}시간"
    return f"{v / (60 * 24):.1f}일"


def _fmt_elapsed_hours(v: float) -> str:
    """[ER:ON compat] 시간 단위 경과값. 48시간을 넘으면 '일'로 바꾼다.

    lab 관찰창 하한이 과거 내원까지 내려가서 "4994.3시간" 같은 값이 실제로 나온다.
    """
    if abs(v) < 48:
        return f"{v:.1f}시간"
    return f"{v / 24:.1f}일"


def _fmt_binary(v: float, yes: str = "해당", no: str = "비해당") -> str:
    return yes if v >= 0.5 else no


def _fmt_vital(base: str, v: float, mode: str = "value") -> str:
    """Vital feature 값 단위 포맷."""
    if mode == "count":
        return f"{v:.0f}회"
    if mode == "fraction":
        return f"{v * 100:.1f}%"
    if mode == "minutes":
        return _fmt_minutes(v)

    if base == "hr":
        if mode == "slope":
            return f"{v:.2f} bpm/시간"
        return f"{v:.1f} bpm" if abs(v - round(v)) > 1e-6 else f"{v:.0f} bpm"

    if base == "rr":
        if mode == "slope":
            return f"{v:.2f}회/분/시간"
        return f"{v:.1f}회/분" if abs(v - round(v)) > 1e-6 else f"{v:.0f}회/분"

    if base == "spo2":
        if mode in {"delta", "spread"}:
            return f"{v:.1f}%p"
        if mode == "slope":
            return f"{v:.2f}%p/시간"
        return f"{v:.1f}%" if abs(v - round(v)) > 1e-6 else f"{v:.0f}%"

    if base in {"sbp", "dbp"}:  # [ER:ON compat] dbp 추가
        if mode == "slope":
            return f"{v:.2f} mmHg/시간"
        return f"{v:.1f} mmHg" if abs(v - round(v)) > 1e-6 else f"{v:.0f} mmHg"

    if base == "temperature":  # [ER:ON compat]
        if mode == "slope":
            return f"{v:.2f}℃/시간"
        return f"{v:.1f}℃"

    return _fmt_num(v)


def _humanize(name: str) -> str:
    return name.replace("_", " ")


# =============================================================================
# 3. Feature parsers
# =============================================================================


def _parse_vital_feature(name: str) -> tuple[str, str, str] | None:
    """(표시명, canonical base, suffix) 반환."""
    # 긴 prefix부터 검사해야 heart_rate가 hr보다 먼저 안전하게 잡힌다.
    for prefix in sorted(VITAL_PREFIXES, key=len, reverse=True):
        token = prefix + "_"
        if name.startswith(token):
            label, base = VITAL_PREFIXES[prefix]
            return label, base, name[len(token):]
    return None


def _parse_lab_feature(name: str) -> tuple[str, str, str] | None:
    """(analyte key, label, suffix) 반환. lab_ prefix도 호환."""
    raw = name[4:] if name.startswith("lab_") else name
    for analyte in sorted(LAB_LABELS, key=len, reverse=True):
        if raw == analyte:
            return analyte, LAB_LABELS[analyte], "value"
        token = analyte + "_"
        if raw.startswith(token):
            return analyte, LAB_LABELS[analyte], raw[len(token):]
    return None


def _is_static_feature(name: str) -> bool:
    return name in STATIC_EXACT or name.startswith(STATIC_PREFIXES)


def _is_uninterpretable_feature(name: str) -> bool:
    # SVD latent dimension은 사람에게 직접 의미를 부여하지 않는다.
    return name.startswith("cc_svd_")



# =============================================================================
# 3-1. Clinical-direction helpers
# =============================================================================


def _direction_from_severity(previous_severity: float, current_severity: float) -> str:
    if current_severity > previous_severity:
        return CLINICAL_WORSENING
    if current_severity < previous_severity:
        return CLINICAL_IMPROVING
    return CLINICAL_NEUTRAL


def _direction_from_monotonic(
    previous_value: float,
    current_value: float,
    higher_is_worse: bool,
) -> str:
    if _same_value(previous_value, current_value):
        return CLINICAL_NEUTRAL

    if higher_is_worse:
        return (
            CLINICAL_WORSENING
            if current_value > previous_value
            else CLINICAL_IMPROVING
        )
    return (
        CLINICAL_WORSENING
        if current_value < previous_value
        else CLINICAL_IMPROVING
    )


def _news2_like_vital_severity(base: str, value: float) -> int | None:
    """성인 일반 환자의 방향 판정용 coarse severity score.

    목적은 NEWS2 자체를 계산하는 것이 아니라, 98→100% SpO2처럼 정상 방향으로
    좋아진 값이 악화 근거로 노출되는 것을 막기 위한 '방향 gate'다.
    특정 환자군(예: hypercapnic respiratory failure)은 별도 threshold config가 필요하다.
    """
    if base == "hr":
        if value <= 40 or value >= 131:
            return 3
        if 111 <= value <= 130:
            return 2
        if 41 <= value <= 50 or 91 <= value <= 110:
            return 1
        if 51 <= value <= 90:
            return 0
        return 1

    if base == "rr":
        if value <= 8 or value >= 25:
            return 3
        if 21 <= value <= 24:
            return 2
        if 9 <= value <= 11:
            return 1
        if 12 <= value <= 20:
            return 0
        return 1

    if base == "spo2":
        if value <= 91:
            return 3
        if value <= 93:
            return 2
        if value <= 95:
            return 1
        return 0

    if base == "sbp":
        if value <= 90 or value >= 220:
            return 3
        if 91 <= value <= 100:
            return 2
        if 101 <= value <= 110:
            return 1
        return 0

    return None


def _temperature_severity(value: float) -> int:
    if value <= 35.0:
        return 3
    if value >= 39.1:
        return 2
    if 35.1 <= value <= 36.0 or 38.1 <= value <= 39.0:
        return 1
    return 0


def _map_severity(value: float) -> int:
    # 단순 방향 gate용. 실제 임상 threshold는 서비스 정책에 맞춰 별도 설정 가능.
    if value < 60 or value > 140:
        return 3
    if value < 65 or value > 130:
        return 2
    if value < 70 or value > 120:
        return 1
    return 0


def _is_nonclinical_proxy_feature(name: str) -> bool:
    lowered = name.lower()
    if any(token in lowered for token in NON_CLINICAL_PROXY_PATTERNS):
        return True
    if lowered.startswith(("cc_svd_", "cc_kw_", "arrival_transport__", "gender__")):
        return True
    return False


def _treatment_escalation_rule(name: str) -> bool:
    lowered = name.lower()
    if not any(token in lowered for token in TREATMENT_ESCALATION_TOKENS):
        return False

    # 치료 종료 후 경과시간/중단 기간은 '치료 강도' 그 자체가 아니다.
    excluded = (
        "time_since",
        "hours_since",
        "minutes_since",
        "off_duration",
        "since_off",
        "weaned",
        "wean_time",
    )
    return not any(token in lowered for token in excluded)


def clinical_change_direction(
    feature: str,
    previous_value: float,
    current_value: float,
) -> dict[str, Any]:
    """exact feature 값 변화의 임상 방향을 판정한다.

    반환 direction:
    - worsening: 사용자 화면의 악화 근거로 노출 가능
    - improving: 개선 방향이므로 악화 근거에서 반드시 제외
    - neutral: 임상 severity level이 동일하여 악화 근거에서 제외
    - unknown: 사전 규칙 미정/비임상 proxy이므로 fail-closed로 제외
    """
    if _same_value(previous_value, current_value):
        return {
            "direction": CLINICAL_NEUTRAL,
            "rule": "no_value_change",
        }

    if _is_static_feature(feature):
        return {
            "direction": CLINICAL_UNKNOWN,
            "rule": "static_feature_not_change_reason",
        }

    if _is_uninterpretable_feature(feature) or _is_nonclinical_proxy_feature(feature):
        return {
            "direction": CLINICAL_UNKNOWN,
            "rule": "nonclinical_or_proxy_feature_excluded",
        }

    # 치료 강도: 0→1 / dose 증가 = worsening, 1→0 / dose 감소 = improving.
    if _treatment_escalation_rule(feature):
        direction = _direction_from_monotonic(
            previous_value,
            current_value,
            higher_is_worse=True,
        )
        return {
            "direction": direction,
            "rule": "treatment_escalation_higher_is_worse",
        }

    # Vital: absolute/summary 값만 severity 기반으로 판정한다.
    parsed_vital = _parse_vital_feature(feature)
    if parsed_vital:
        _label, base, suffix = parsed_vital

        # fraction_abnormal은 증가할수록 악화 방향.
        if suffix == "fraction_abnormal_6h":
            return {
                "direction": _direction_from_monotonic(
                    previous_value,
                    current_value,
                    higher_is_worse=True,
                ),
                "rule": "vital_abnormal_fraction_higher_is_worse",
            }

        # 측정 횟수, 경과시간, slope/delta/std는 생리학적 방향을 단순 부호로 단정하지 않는다.
        allowed_absolute_suffixes = {
            "last",
            "min_6h",
            "max_6h",
            "mean_6h",
            "mean",
            "min",
            "max",
        }
        if suffix not in allowed_absolute_suffixes:
            return {
                "direction": CLINICAL_UNKNOWN,
                "rule": "vital_trajectory_or_workflow_feature_not_auto_interpreted",
            }

        prev_sev = _news2_like_vital_severity(base, previous_value)
        cur_sev = _news2_like_vital_severity(base, current_value)
        if prev_sev is None or cur_sev is None:
            return {
                "direction": CLINICAL_UNKNOWN,
                "rule": "vital_rule_not_defined",
            }

        return {
            "direction": _direction_from_severity(prev_sev, cur_sev),
            "rule": f"{base}_severity_gate",
            "previous_severity": prev_sev,
            "current_severity": cur_sev,
        }

    # Lab: direction이 명확한 analyte만 허용한다.
    parsed_lab = _parse_lab_feature(feature)
    if parsed_lab:
        analyte, _label, suffix = parsed_lab
        if suffix not in LAB_DIRECTIONAL_SUFFIXES:
            return {
                "direction": CLINICAL_UNKNOWN,
                "rule": "lab_suffix_not_auto_interpreted",
            }

        if analyte in LAB_HIGHER_WORSE:
            return {
                "direction": _direction_from_monotonic(
                    previous_value,
                    current_value,
                    higher_is_worse=True,
                ),
                "rule": "lab_higher_is_worse",
            }
        if analyte in LAB_LOWER_WORSE:
            return {
                "direction": _direction_from_monotonic(
                    previous_value,
                    current_value,
                    higher_is_worse=False,
                ),
                "rule": "lab_lower_is_worse",
            }
        return {
            "direction": CLINICAL_UNKNOWN,
            "rule": "lab_direction_not_defined_fail_closed",
        }

    # Derived / binary clinical states.
    if feature in {
        "abn_hr",
        "abn_rr",
        "abn_spo2",
        "abn_sbp",
        "abn_temperature",
        "shock_pattern",
        "respiratory_failure_pattern",
        "silent_hypoxemia_pattern",
    }:
        return {
            "direction": _direction_from_monotonic(
                previous_value,
                current_value,
                higher_is_worse=True,
            ),
            "rule": "binary_clinical_abnormality_higher_is_worse",
        }

    if feature in {
        "abnormal_vital_count",
        "multi_system_abnormal_count",
        "shock_index",
        "modified_shock_index",
        "nlr",
    }:
        return {
            "direction": _direction_from_monotonic(
                previous_value,
                current_value,
                higher_is_worse=True,
            ),
            "rule": "derived_higher_is_worse",
        }

    if feature == "temperature_last":
        prev_sev = _temperature_severity(previous_value)
        cur_sev = _temperature_severity(current_value)
        return {
            "direction": _direction_from_severity(prev_sev, cur_sev),
            "rule": "temperature_severity_gate",
            "previous_severity": prev_sev,
            "current_severity": cur_sev,
        }

    if feature in {"map_last", "map_derived_or_measured"}:
        prev_sev = _map_severity(previous_value)
        cur_sev = _map_severity(current_value)
        return {
            "direction": _direction_from_severity(prev_sev, cur_sev),
            "rule": "map_severity_gate",
            "previous_severity": prev_sev,
            "current_severity": cur_sev,
        }

    # 알 수 없는 feature는 안전하게 노출하지 않는다.
    return {
        "direction": CLINICAL_UNKNOWN,
        "rule": "no_clinical_direction_rule_fail_closed",
    }


# =============================================================================
# 4. Exact feature formatting
# =============================================================================


def _vital_feature_label_and_formatter(name: str):
    parsed = _parse_vital_feature(name)
    if not parsed:
        return None

    vital_label, base, suffix = parsed

    if suffix == "last":
        return vital_label, lambda v: _fmt_vital(base, v)
    if suffix == "min_6h":
        return f"{vital_label} 6시간 최저", lambda v: _fmt_vital(base, v)
    if suffix == "max_6h":
        return f"{vital_label} 6시간 최고", lambda v: _fmt_vital(base, v)
    if suffix == "mean_6h":
        return f"{vital_label} 6시간 평균", lambda v: _fmt_vital(base, v)
    if suffix == "std_6h":
        return f"{vital_label} 6시간 변동성", lambda v: _fmt_vital(base, v, "spread")

    m = re.fullmatch(r"n_measure_(1|3|6)h", suffix)
    if m:
        h = m.group(1)
        return f"{vital_label} 최근 {h}시간 측정 횟수", lambda v: _fmt_vital(base, v, "count")

    if suffix == "fraction_abnormal_6h":
        return f"{vital_label} 6시간 이상값 비율", lambda v: _fmt_vital(base, v, "fraction")
    if suffix == "delta_t_min":
        return f"{vital_label} 마지막 측정 후 경과", lambda v: _fmt_vital(base, v, "minutes")
    if suffix == "time_since_last_normal_min":
        return f"{vital_label} 마지막 정상값 이후 경과", lambda v: _fmt_vital(base, v, "minutes")
    if suffix == "time_since_first_abnormal_6h_min":
        return f"{vital_label} 첫 이상값 이후 경과", lambda v: _fmt_vital(base, v, "minutes")
    if suffix == "delta_6h":
        return f"{vital_label} 6시간 변화량", lambda v: _fmt_vital(base, v, "delta")
    if suffix == "slope_per_h":
        return f"{vital_label} 시간당 변화율", lambda v: _fmt_vital(base, v, "slope")
    if suffix == "worst_minus_last":
        return f"{vital_label} 6시간 최악값-최근값 차이", lambda v: _fmt_vital(base, v, "spread")
    if suffix == "last_minus_mean":
        return f"{vital_label} 최근값-6시간 평균 차이", lambda v: _fmt_vital(base, v, "spread")

    # 과거 naming 호환: *_mean, *_min, *_max, *_slope, *_n, *_dt
    if suffix == "mean":
        return f"{vital_label} 평균", lambda v: _fmt_vital(base, v)
    if suffix == "min":
        return f"{vital_label} 최저", lambda v: _fmt_vital(base, v)
    if suffix == "max":
        return f"{vital_label} 최고", lambda v: _fmt_vital(base, v)
    if suffix == "slope":
        return f"{vital_label} 변화율", lambda v: _fmt_vital(base, v, "slope")
    if suffix == "n":
        return f"{vital_label} 측정 횟수", lambda v: _fmt_vital(base, v, "count")
    if suffix == "dt":
        # [ER:ON compat] 구 naming(*_dt)의 단위는 **시간**이다(online_features.py 가 h 로 만든다).
        # 새 naming(*_delta_t_min)만 분 단위다. 분으로 읽으면 3.2시간이 "3분"이 된다.
        return f"{vital_label} 마지막 측정 후 경과", _fmt_elapsed_hours

    return f"{vital_label} ({suffix})", lambda v: _fmt_num(v)


def _lab_feature_label_and_formatter(name: str):
    parsed = _parse_lab_feature(name)
    if not parsed:
        return None

    _analyte, label, suffix = parsed

    if suffix in {"value", "last"}:
        return label, _fmt_num
    if suffix == "first_24h":
        return f"{label} 초기 24시간 첫값", _fmt_num
    if suffix in {"delta_24h", "change_24h"}:
        return f"{label} 24시간 변화량", _fmt_num
    if suffix == "clearance_like_pct_24h":
        return f"{label} 24시간 clearance-like", lambda v: f"{v:.1f}%"
    if suffix == "change_rate_per_h":
        return f"{label} 시간당 변화율", lambda v: f"{_fmt_num(v)}/시간"
    if suffix == "dt":
        # 과거 v2 lab_*_dt 호환: 값의 단위가 hour였음.
        return f"최근 {label} 검사 후 경과", _fmt_elapsed_hours  # [ER:ON compat]
    if suffix == "mask":
        return f"{label} 검사 상태", lambda v: _fmt_binary(v, "확인됨", "미확인")

    return f"{label} ({suffix})", _fmt_num


def _triage_label_and_formatter(name: str):
    if not name.startswith("triage_"):
        return None

    key = name[len("triage_"):]
    if key.endswith("_missing"):
        base = key[:-len("_missing")]
        label = TRIAGE_LABELS.get(base, f"초기 {_humanize(base)}")
        return f"{label} 기록 상태", lambda v: _fmt_binary(v, "미기록", "기록됨")

    if key.startswith("abn_"):
        base = key[len("abn_"):]
        labels = {
            "hr": "초기 심박수 이상 여부",
            "rr": "초기 호흡수 이상 여부",
            "spo2": "초기 산소포화도 이상 여부",
            "sbp": "초기 수축기 혈압 이상 여부",
            "temperature": "초기 체온 이상 여부",
        }
        return labels.get(base, f"초기 {_humanize(base)} 이상 여부"), _fmt_binary

    if key == "abnormal_vital_count":
        return "초기 비정상 활력징후 수", lambda v: f"{v:.0f}개"
    if key == "abnormal_vital_n_unknown":
        return "초기 미측정 활력징후 수", lambda v: f"{v:.0f}개"

    label = TRIAGE_LABELS.get(key, f"초기 {_humanize(key)}")
    # [ER:ON compat] 구 naming(heartrate/resprate/o2sat/temperature)도 같은 단위로 쓴다.
    if key in {"hr", "heartrate"}:
        return label, lambda v: _fmt_vital("hr", v)
    if key in {"rr", "resprate"}:
        return label, lambda v: _fmt_vital("rr", v)
    if key in {"spo2", "o2sat"}:
        return label, lambda v: _fmt_vital("spo2", v)
    if key in {"sbp", "dbp", "map", "pulse_pressure"}:
        return label, lambda v: f"{v:.0f} mmHg"
    if key in {"temperature_c", "temperature"}:
        return label, lambda v: f"{v:.1f}℃"
    if key == "acuity":
        return label, lambda v: f"{v:.0f}"
    return label, _fmt_num


def _derived_label_and_formatter(name: str):
    if name in DERIVED_LABELS:
        label = DERIVED_LABELS[name]

        if name in {"abn_hr", "abn_rr", "abn_spo2", "abn_sbp", "abn_temperature"}:
            return label, _fmt_binary
        if name in {"shock_pattern", "respiratory_failure_pattern", "silent_hypoxemia_pattern"}:
            return label, _fmt_binary
        if name in {
            "abnormal_vital_count",
            "abnormal_vital_n_unknown",
            "multi_system_abnormal_count",
            "abnormal_vital_persistence",  # [ER:ON compat]
            "abnormal_vital_delta",        # [ER:ON compat]
        }:
            return label, lambda v: f"{v:.0f}개"
        if name == "map_est":  # [ER:ON compat]
            return label, lambda v: f"{v:.0f} mmHg"
        if name in {"dbp_last", "map_last", "map_derived_or_measured", "pulse_pressure"}:
            return label, lambda v: f"{v:.0f} mmHg"
        if name == "temperature_last":
            return label, lambda v: f"{v:.1f}℃"
        return label, _fmt_num

    if name == "hours_from_ed" or name == "hours_since_ed_arrival":
        return "응급실 도착 후 경과", lambda v: f"{v:.1f}시간"

    if name == "prior_ed_visit_count":
        return "과거 응급실 방문 횟수", lambda v: f"{v:.0f}회"
    if name == "hours_since_prior_ed":
        return "직전 응급실 방문 후 경과", lambda v: f"{v:.1f}시간"

    return None


def _onehot_or_cc_label_and_formatter(name: str, value: float):
    if name.startswith("arrival_transport__"):
        if value < 0.5:
            return None
        category = name.split("__", 1)[1].replace("_2", "").replace("_", " ").upper()
        return "내원수단", lambda _v: category

    if name.startswith("gender__"):
        if value < 0.5:
            return None
        category = name.split("__", 1)[1].upper()
        return "성별", lambda _v: category

    if name.startswith("cc_kw_"):
        if value < 0.5:
            return None
        key = name[len("cc_kw_"):]
        return "주호소 키워드", lambda _v: CC_KEYWORD_LABELS.get(key, _humanize(key))

    if name == "cc_missing":
        return "주호소 기록 상태", lambda v: _fmt_binary(v, "미기록", "기록됨")
    if name == "cc_len":
        return "주호소 텍스트 길이", lambda v: f"{v:.0f}"

    return None


def feature_display(name: str, value: float) -> tuple[str, str] | None:
    """정확한 feature 하나를 사람이 읽을 수 있는 label/value로 변환."""
    if _is_uninterpretable_feature(name):
        return None

    for resolver in (
        _vital_feature_label_and_formatter,
        _lab_feature_label_and_formatter,
        _triage_label_and_formatter,
        _derived_label_and_formatter,
    ):
        resolved = resolver(name)
        if resolved:
            label, formatter = resolved
            return label, formatter(value)

    onehot = _onehot_or_cc_label_and_formatter(name, value)
    if onehot:
        label, formatter = onehot
        return label, formatter(value)

    # 비활성 one-hot/keyword feature(value=0)는 사용자 문구로 노출하지 않는다.
    if name.startswith(("arrival_transport__", "gender__", "cc_kw_")):
        return None

    if name == "age":
        return "연령", f"{value:.0f}세"
    if name.endswith("_missing"):
        return _humanize(name[:-len("_missing")]), _fmt_binary(value, "미기록", "기록됨")
    if name in BINARY_FEATURES:
        return _humanize(name), _fmt_binary(value)

    # 해석 가능한 명시적 수치 feature는 raw feature명도 함께 보존한다.
    return _humanize(name), _fmt_num(value)


# =============================================================================
# 5. Current-risk explanation: exact feature unit
# =============================================================================


def build_reason(
    feature_names: list[str],
    contributions: np.ndarray,
    row: dict[str, Any],
    max_reasons: int = 2,
):
    """현재 위험도를 높이는 양의 contribution 상위 feature를 정확히 표시한다."""
    contrib = np.asarray(contributions, dtype=float)
    if contrib.ndim != 1 or len(contrib) != len(feature_names):
        raise ValueError(
            f"Contribution shape mismatch: {contrib.shape} vs {len(feature_names)} features"
        )

    ranked = sorted(
        (
            (i, feature_names[i], float(contrib[i]))
            for i in range(len(feature_names))
            if np.isfinite(contrib[i]) and float(contrib[i]) > 0
        ),
        key=lambda x: x[2],
        reverse=True,
    )

    out = []
    for _i, feature, score in ranked:
        value = _value(row, feature)
        if value is None:
            continue

        display = feature_display(feature, value)
        if not display:
            continue

        label, formatted = display
        out.append(
            {
                "feature": feature,
                "feature_label": label,
                "text": f"{label} {formatted}",
                "value": value,
                "contribution": score,
                "contribution_space": "lightgbm_raw_score_shap",
            }
        )
        if len(out) >= max_reasons:
            break

    return out


# =============================================================================
# 6. Change explanation: exact delta-contribution feature unit
# =============================================================================


def describe_feature_change(
    feature: str,
    previous_row: dict[str, Any],
    current_row: dict[str, Any],
) -> dict[str, Any] | None:
    """동일한 model feature의 이전값과 현재값을 정확히 비교한다."""
    prev_value = _value(previous_row, feature)
    cur_value = _value(current_row, feature)

    if prev_value is None or cur_value is None:
        return None
    if _same_value(prev_value, cur_value):
        return None

    prev_display = feature_display(feature, prev_value)
    cur_display = feature_display(feature, cur_value)
    if not prev_display or not cur_display:
        return None

    prev_label, prev_text = prev_display
    cur_label, cur_text = cur_display

    # [ER:ON compat] 표시 문자열이 같으면 화면에서는 변화가 아니다.
    # 예: 심박수 88.0 → 88.4 는 "88 bpm → 88 bpm" 으로 찍힌다.
    if prev_text == cur_text:
        return None

    label = cur_label if cur_label == prev_label else cur_label

    return {
        "feature": feature,
        "feature_label": label,
        "previous_value": prev_value,
        "current_value": cur_value,
        "text": f"{label} {prev_text} → {cur_text}",
    }


def build_change_reason(
    feature_names: list[str],
    previous_contributions: np.ndarray,
    current_contributions: np.ndarray,
    previous_row: dict[str, Any],
    current_row: dict[str, Any],
    max_reasons: int = 2,
    min_delta_contribution: float = 0.0,
):
    """직전 대비 '임상적 악화 방향'의 positive Δcontribution만 설명한다.

    사용자 화면 노출 조건 (모두 만족해야 함)
    ----------------------------------------
    1) 동일 exact model feature의 Δcontribution > min_delta_contribution
    2) 그 exact feature의 실제 값이 직전 시점과 달라짐
    3) static/latent/workflow proxy가 아님
    4) clinical_change_direction(...) == "worsening"

    따라서 아래와 같은 개선 변화는 모델 Δcontribution이 양수더라도 절대 악화 근거로 노출하지 않는다.
    - SpO2 98 -> 100
    - vasopressor 1 -> 0
    - lactate 4.0 -> 2.0
    - albumin 3.0 -> 4.0

    방향 규칙이 없는 feature도 fail-closed로 제외한다.
    """
    prev = np.asarray(previous_contributions, dtype=float)
    cur = np.asarray(current_contributions, dtype=float)

    if prev.shape != cur.shape:
        raise ValueError(f"Contribution shape mismatch: {prev.shape} vs {cur.shape}")
    if prev.ndim != 1 or len(prev) != len(feature_names):
        raise ValueError(
            f"Contribution shape mismatch: {prev.shape} vs {len(feature_names)} features"
        )

    delta = cur - prev

    ranked = sorted(
        (
            (i, feature_names[i], float(delta[i]))
            for i in range(len(feature_names))
            if np.isfinite(delta[i]) and float(delta[i]) > min_delta_contribution
        ),
        key=lambda x: x[2],
        reverse=True,
    )

    out = []
    for i, feature, delta_score in ranked:
        if _is_static_feature(feature) or _is_uninterpretable_feature(feature):
            continue

        change = describe_feature_change(feature, previous_row, current_row)
        if not change:
            continue

        clinical = clinical_change_direction(
            feature=feature,
            previous_value=change["previous_value"],
            current_value=change["current_value"],
        )

        # FINAL safety gate: worsening만 사용자 악화 신호로 통과.
        if clinical["direction"] != CLINICAL_WORSENING:
            continue

        change.update(
            {
                "previous_contribution": float(prev[i]),
                "current_contribution": float(cur[i]),
                "delta_contribution": delta_score,
                "contribution_space": "lightgbm_raw_score_shap",
                "clinical_direction": clinical["direction"],
                "clinical_rule": clinical["rule"],
                "clinical_gate_passed": True,
            }
        )
        if "previous_severity" in clinical:
            change["previous_clinical_severity"] = clinical["previous_severity"]
        if "current_severity" in clinical:
            change["current_clinical_severity"] = clinical["current_severity"]

        out.append(change)
        if len(out) >= max_reasons:
            break

    return out


# =============================================================================
# 7. QA/debug helper
# =============================================================================


def top_delta_contributions(
    feature_names: list[str],
    previous_contributions: np.ndarray,
    current_contributions: np.ndarray,
    previous_row: dict[str, Any] | None = None,
    current_row: dict[str, Any] | None = None,
    top_n: int = 20,
):
    """개발/QA용: Δcontribution 상위 feature와 clinical gate 판정을 반환한다.

    이 payload는 사용자 화면용이 아니다. improving/neutral/unknown도 모두 남겨
    '왜 악화 신호에서 제외되었는지' 확인할 수 있게 한다.
    """
    prev = np.asarray(previous_contributions, dtype=float)
    cur = np.asarray(current_contributions, dtype=float)
    if prev.shape != cur.shape or prev.ndim != 1 or len(prev) != len(feature_names):
        raise ValueError("Contribution shape mismatch")

    delta = cur - prev
    order = np.argsort(np.abs(delta))[::-1][:top_n]

    out = []
    for i in order:
        feature = feature_names[int(i)]
        prev_value = _value(previous_row, feature) if previous_row is not None else None
        cur_value = _value(current_row, feature) if current_row is not None else None

        item = {
            "feature": feature,
            "previous_contribution": float(prev[i]),
            "current_contribution": float(cur[i]),
            "delta_contribution": float(delta[i]),
            "previous_value": prev_value,
            "current_value": cur_value,
        }

        if prev_value is not None and cur_value is not None:
            clinical = clinical_change_direction(feature, prev_value, cur_value)
            item["clinical_direction"] = clinical["direction"]
            item["clinical_rule"] = clinical["rule"]
            item["clinical_gate_passed"] = (
                float(delta[i]) > 0
                and clinical["direction"] == CLINICAL_WORSENING
                and not _same_value(prev_value, cur_value)
            )
            if "previous_severity" in clinical:
                item["previous_clinical_severity"] = clinical["previous_severity"]
            if "current_severity" in clinical:
                item["current_clinical_severity"] = clinical["current_severity"]

        out.append(item)
    return out

