from __future__ import annotations

import copy
from typing import Any, Iterable


# Reused from the team-provided clinical_sanitizer.py. These rules report
# possible field errors; they never remove or rewrite the extracted value.
NON_PAIN_SYMPTOMS = frozenset(
    {
        "어지러움",
        "어지럼",
        "구토",
        "오심",
        "발열",
        "오한",
        "호흡곤란",
        "토혈",
    }
)

ROS_FORBIDDEN_KEYWORDS = (
    "수술",
    "수술력",
    "알레르기",
    "약물",
    "복용약",
    "복용하는 약",
    "복용 중인 약",
    "정기적으로 복용",
    "정기적으로 먹는 약",
    "간질환",
    "위궤양",
    "당뇨",
    "고혈압",
    "과거력",
)

ALLERGY_KEYWORDS = ("알레르기", "알러지", "allergy")


def _atomic_values(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _atomic_values(item)
        return
    if not isinstance(value, dict):
        return
    if "status" in value:
        yield value
        return
    for item in value.values():
        yield from _atomic_values(item)


def _issue(
    *,
    field_id: str,
    value: dict[str, Any],
    reason: str,
    message: str,
) -> dict[str, Any]:
    return {
        "code": "FIELD_MISCLASSIFICATION",
        "severity": "REVIEW_REQUIRED",
        "field_id": field_id,
        "value": str(value.get("raw_value") or ""),
        "reason": reason,
        "message": message,
        "evidence": copy.deepcopy(value.get("evidence")),
    }


def field_misclassification_issues(
    clinical_record: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return non-destructive field-classification issues for clinician review."""

    issues: list[dict[str, Any]] = []

    for value in _atomic_values(clinical_record.get("pain_assessment")):
        raw_value = str(value.get("raw_value") or "").strip(" .,!?")
        if raw_value in NON_PAIN_SYMPTOMS:
            issues.append(
                _issue(
                    field_id="pain_assessment",
                    value=value,
                    reason="NON_PAIN_SYMPTOM_IN_PAIN_ASSESSMENT",
                    message="비통증 증상이 통증평가에 배치되었습니다.",
                )
            )

    for value in _atomic_values(clinical_record.get("review_of_systems")):
        raw_value = str(value.get("raw_value") or "")
        if any(keyword in raw_value for keyword in ROS_FORBIDDEN_KEYWORDS):
            issues.append(
                _issue(
                    field_id="review_of_systems",
                    value=value,
                    reason="NON_ROS_INFORMATION_IN_REVIEW_OF_SYSTEMS",
                    message="병력·약물·알레르기 정보가 계통문진에 배치되었습니다.",
                )
            )

    for value in _atomic_values(clinical_record.get("medications")):
        raw_value = str(value.get("raw_value") or "").casefold()
        if any(keyword in raw_value for keyword in ALLERGY_KEYWORDS):
            issues.append(
                _issue(
                    field_id="medications",
                    value=value,
                    reason="ALLERGY_INFORMATION_IN_MEDICATIONS",
                    message="알레르기 정보가 복용약에 배치되었습니다.",
                )
            )

    return issues

