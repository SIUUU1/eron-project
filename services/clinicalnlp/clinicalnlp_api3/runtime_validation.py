from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from .field_classification import field_misclassification_issues
from .policy_retrieval import retrieve_policy_evidence


_DEFAULT_GUARDRAIL_RULES = (
    Path(__file__).parents[1]
    / "docs"
    / "ERON_Guardrail_Rules_v1"
    / "ERON_Guardrail_Rules_v1.json"
)
_DEFAULT_VALIDATION_THRESHOLDS = (
    Path(__file__).parents[1]
    / "docs"
    / "ERON_Validation_Thresholds_v1"
    / "ERON_Validation_Thresholds_v1.json"
)
DEFAULT_GUARDRAIL_RULES = Path(
    os.environ.get("CLINICALNLP_GUARDRAIL_RULES", _DEFAULT_GUARDRAIL_RULES)
)
DEFAULT_VALIDATION_THRESHOLDS = Path(
    os.environ.get(
        "CLINICALNLP_VALIDATION_THRESHOLDS",
        _DEFAULT_VALIDATION_THRESHOLDS,
    )
)
VITAL_RANGES = {
    "blood_pressure_systolic": {
        "label": "수축기혈압",
        "minimum": 20,
        "maximum": 300,
        "unit": "mmHg",
    },
    "blood_pressure_diastolic": {
        "label": "이완기혈압",
        "minimum": 10,
        "maximum": 200,
        "unit": "mmHg",
    },
    "heart_rate": {
        "label": "심박수",
        "minimum": 20,
        "maximum": 300,
        "unit": "bpm",
    },
    "respiratory_rate": {
        "label": "호흡수",
        "minimum": 4,
        "maximum": 80,
        "unit": "breaths/min",
    },
    "oxygen_saturation": {
        "label": "산소포화도",
        "minimum": 0,
        "maximum": 100,
        "unit": "%",
    },
    "body_temperature": {
        "label": "체온",
        "minimum": 25,
        "maximum": 45,
        "unit": "°C",
    },
}
PROCESSING_STATUS_VALUES = frozenset({"completed", "partial", "failed"})
RECORD_STATUS_VALUES = frozenset({"NOT_STARTED", "DRAFT", "COMPLETED"})
VALIDATION_STATUS_VALUES = frozenset({"PASS", "REVIEW_REQUIRED", "BLOCK"})
INFORMATION_STATUS_VALUES = frozenset(
    {"PRESENT", "NONE", "NOT_ASSESSED", "UNCERTAIN"}
)
RUNTIME_THRESHOLD_BY_RULE = {
    "G01": "V13",
    "G06": "V13",
    "G07": "V13",
    "G08": "V15",
    "G09": "V16",
    "G19": "V23",
}


def _load_json(path: Path | str) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"validation policy must be an object: {path}")
    return value


def _rules_by_id(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(rule["rule_id"]): rule
        for rule in policy.get("rules", [])
        if isinstance(rule, dict) and rule.get("rule_id")
    }


def _attach_runtime_thresholds(
    rules: dict[str, dict[str, Any]],
    thresholds: dict[str, Any],
) -> None:
    metrics = {
        str(metric["metric_id"]): metric
        for metric in thresholds.get("metrics", [])
        if isinstance(metric, dict) and metric.get("metric_id")
    }
    for rule_id, metric_id in RUNTIME_THRESHOLD_BY_RULE.items():
        metric = metrics.get(metric_id)
        if rule_id in rules and metric is not None:
            rules[rule_id]["_runtime_threshold"] = metric


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


def _compact(value: Any) -> str:
    return "".join(re.findall(r"[0-9a-z가-힣]+", str(value or "").casefold()))


def _segments(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    api3 = document.get("api3") if isinstance(document.get("api3"), dict) else {}
    return {
        str(segment.get("id")): segment
        for segment in api3.get("segments", [])
        if isinstance(segment, dict) and segment.get("id") is not None
    }


def _atom_is_grounded(
    atom: dict[str, Any],
    segments: dict[str, dict[str, Any]],
) -> bool:
    evidence = atom.get("evidence")
    if not isinstance(evidence, dict):
        return False
    if evidence.get("source_type") == "structured_patient_data":
        return True
    segment_id = evidence.get("source_segment_id") or evidence.get("segment_id")
    segment = segments.get(str(segment_id)) if segment_id is not None else None
    if segment is None:
        start = evidence.get("start")
        end = evidence.get("end")
        evidence_text = str(evidence.get("text") or "").strip()
        candidates = list(segments.values())
        if start is not None and end is not None:
            candidates = [
                candidate
                for candidate in candidates
                if candidate.get("start") == start and candidate.get("end") == end
            ]
        if evidence_text:
            candidates = [
                candidate
                for candidate in candidates
                if evidence_text
                in {
                    str(candidate.get("text") or "").strip(),
                    str(candidate.get("raw_text") or "").strip(),
                    str(candidate.get("corrected_text") or "").strip(),
                }
            ]
        if len(candidates) == 1 and (
            (start is not None and end is not None) or evidence_text
        ):
            segment = candidates[0]
    if segment is None:
        return False
    source_text = segment.get("raw_text") or segment.get("text")
    raw_value = atom.get("raw_value")
    compact_value = _compact(raw_value)
    return bool(compact_value and compact_value in _compact(source_text))


def _draft_field(document: dict[str, Any], field_id: str) -> dict[str, Any]:
    draft = document.get("draft") if isinstance(document.get("draft"), dict) else {}
    fields = draft.get("fields") if isinstance(draft.get("fields"), dict) else {}
    field = fields.get(field_id)
    if field_id == "allergy" and field is None:
        field = fields.get("drug_allergy")
    return field if isinstance(field, dict) else {}


def _is_question_text(value: Any) -> bool:
    text = str(value or "").strip()
    if "?" in text:
        return not bool(text.rsplit("?", 1)[1].strip())
    return bool(
        re.search(r"(?:나요|가요|까요|습니까|있으세요|없으세요)[.!]?\s*$", text)
    )


def _field_evidence_texts(
    field: dict[str, Any],
    segments: dict[str, dict[str, Any]],
) -> list[str]:
    texts: list[str] = []
    evidence_values = field.get("evidence")
    if not isinstance(evidence_values, list):
        return texts
    for evidence in evidence_values:
        if not isinstance(evidence, dict):
            continue
        segment_id = evidence.get("segment_id") or evidence.get("source_segment_id")
        segment = segments.get(str(segment_id))
        text = (
            (segment.get("raw_text") or segment.get("text"))
            if segment is not None
            else evidence.get("raw_text") or evidence.get("text")
        )
        normalized = str(text or "").strip()
        if normalized and normalized not in texts:
            texts.append(normalized)
    return texts


def _is_explicit_negation(value: Any) -> bool:
    text = str(value or "").strip()
    if not text or _is_question_text(text):
        return False
    return bool(
        re.search(
            r"(?:없(?:습니다|어요|다|음|었)|아니(?:요|다|었습니다)|"
            r"하지\s*않|안\s+\S+|부인)",
            text,
        )
    )


def _is_uncertain_text(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(
        re.search(
            r"(?:모르|기억(?:이)?\s*(?:안|못)|확실하지|불확실|정확하지|"
            r"것\s*같|듯(?:해|합니|하|$)|아마|추정|가능성)",
            text,
        )
    )


def _assertion_terms(value: Any) -> set[str]:
    text = str(value or "").casefold()
    text = re.sub(
        r"(?:있(?:습니다|어요|다|음)|없(?:습니다|어요|다|음|었(?:습니다|어요|다)?)|"
        r"아니(?:요|다|었습니다)|하지\s*않(?:습니다|아요|다|음)?|"
        r"부인(?:합니다|함)?)",
        " ",
        text,
    )
    terms: set[str] = set()
    for token in re.findall(r"[0-9a-z가-힣]+", text):
        token = re.sub(r"(?:은|는|이|가|을|를|도)$", "", token)
        if token and token not in {"특별히", "현재", "병력", "과거력"}:
            terms.add(token)
    return terms


def _has_contradictory_evidence(texts: list[str]) -> bool:
    positive = [
        _assertion_terms(text) for text in texts if not _is_explicit_negation(text)
    ]
    negative = [
        _assertion_terms(text) for text in texts if _is_explicit_negation(text)
    ]
    return any(
        left and right and bool(left & right)
        for left in positive
        for right in negative
    )


def _clinical_source(document: dict[str, Any], field_id: str) -> Any:
    api2 = document.get("api2") if isinstance(document.get("api2"), dict) else {}
    record = (
        api2.get("clinical_record")
        if isinstance(api2.get("clinical_record"), dict)
        else {}
    )
    return record.get(field_id)


def _field_has_unsupported_atoms(document: dict[str, Any], field_id: str) -> bool:
    atoms = [
        atom
        for atom in _atomic_values(_clinical_source(document, field_id))
        if atom.get("raw_value") not in (None, "")
        and atom.get("status") in {"confirmed", "needs_confirmation"}
    ]
    if not atoms:
        return False
    segments = _segments(document)
    return any(not _atom_is_grounded(atom, segments) for atom in atoms)


def _issue(
    rule: dict[str, Any],
    *,
    field_id: str,
    message: str,
    evidence: list[dict[str, Any]],
    suggested_action: str,
) -> dict[str, Any]:
    threshold = rule.get("_runtime_threshold")
    zero_tolerance = (
        isinstance(threshold, dict)
        and str(threshold.get("provisional_threshold") or "").replace(" ", "")
        in {"=0", "=0.0", "=0.00"}
        and threshold.get("release_gate") == "YES"
    )
    severity = (
        "BLOCK"
        if zero_tolerance or rule.get("severity") != "WARN"
        else "REVIEW_REQUIRED"
    )
    issue = {
        "rule_id": str(rule["rule_id"]),
        "severity": severity,
        "field_id": field_id,
        "message": message,
        "evidence": copy.deepcopy(evidence),
        "suggested_action": suggested_action,
        "policy_evidence": [],
        "policy_evidence_status": "unavailable",
    }
    if isinstance(threshold, dict):
        issue["threshold_id"] = threshold.get("metric_id")
        issue["threshold"] = threshold.get("provisional_threshold")
    return issue


def _system_issue(
    *,
    rule_id: str,
    severity: str,
    field_id: str,
    message: str,
    evidence: list[dict[str, Any]],
    suggested_action: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "field_id": field_id,
        "message": message,
        "evidence": copy.deepcopy(evidence),
        "suggested_action": suggested_action,
        "policy_evidence": [],
        "policy_evidence_status": "not_applicable",
        **details,
    }


def _measurement_evidence(
    segment: dict[str, Any],
    annotation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "segment_id": segment.get("id"),
        "start": segment.get("start"),
        "end": segment.get("end"),
        "raw_text": segment.get("raw_text") or segment.get("text"),
        "corrected_text": segment.get("corrected_text"),
        "source_span": copy.deepcopy(annotation.get("source_span") or {}),
    }


def _vital_issues(document: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    api3 = document.get("api3") if isinstance(document.get("api3"), dict) else {}
    for segment in api3.get("segments", []):
        if not isinstance(segment, dict):
            continue
        for annotation in segment.get("annotations", []):
            if (
                not isinstance(annotation, dict)
                or annotation.get("type") != "numeric_measurement_candidate"
            ):
                continue
            raw_value = str((annotation.get("source_span") or {}).get("text") or "")
            for candidate in annotation.get("candidates", []):
                if not isinstance(candidate, dict):
                    continue
                kind = str(candidate.get("kind") or "")
                values: list[tuple[str, Any]]
                if kind == "blood_pressure":
                    values = [
                        ("blood_pressure_systolic", candidate.get("systolic")),
                        ("blood_pressure_diastolic", candidate.get("diastolic")),
                    ]
                else:
                    values = [(kind, candidate.get("value"))]
                for range_key, value in values:
                    rule = VITAL_RANGES.get(range_key)
                    if rule is None or isinstance(value, bool) or not isinstance(
                        value, (int, float)
                    ):
                        continue
                    numeric = float(value)
                    if rule["minimum"] <= numeric <= rule["maximum"]:
                        continue
                    display_value: int | float = int(numeric) if numeric.is_integer() else numeric
                    issues.append(
                        _system_issue(
                            rule_id="VITAL_RANGE",
                            severity="BLOCK",
                            field_id="physical_examination",
                            message=(
                                f"{rule['label']} {display_value}{rule['unit']}는 "
                                "허용 범위를 벗어났습니다."
                            ),
                            evidence=[_measurement_evidence(segment, annotation)],
                            suggested_action=(
                                "원음 또는 모니터값을 확인하고 값을 수정한 뒤 "
                                "재검증해 주세요."
                            ),
                            extracted_value=display_value,
                            raw_value=raw_value,
                            allowed_range={
                                "minimum": rule["minimum"],
                                "maximum": rule["maximum"],
                                "unit": rule["unit"],
                            },
                        )
                    )
                if kind == "blood_pressure":
                    systolic = candidate.get("systolic")
                    diastolic = candidate.get("diastolic")
                    if (
                        isinstance(systolic, (int, float))
                        and not isinstance(systolic, bool)
                        and isinstance(diastolic, (int, float))
                        and not isinstance(diastolic, bool)
                        and systolic <= diastolic
                    ):
                        issues.append(
                            _system_issue(
                                rule_id="BP_RELATION",
                                severity="REVIEW_REQUIRED",
                                field_id="physical_examination",
                                message=(
                                    "수축기혈압이 이완기혈압보다 높지 않습니다."
                                ),
                                evidence=[_measurement_evidence(segment, annotation)],
                                suggested_action=(
                                    "원음 또는 모니터값을 확인하고 혈압 값을 수정한 "
                                    "뒤 재검증해 주세요."
                                ),
                                extracted_value={
                                    "systolic": systolic,
                                    "diastolic": diastolic,
                                },
                                raw_value=raw_value,
                                allowed_range={
                                    "relation": "systolic > diastolic",
                                    "unit": "mmHg",
                                },
                            )
                        )
    return issues


def _enum_issues(
    document: dict[str, Any],
    fields: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    checks = (
        ("processing_status", document.get("processing_status"), PROCESSING_STATUS_VALUES),
        ("record_status", document.get("record_status"), RECORD_STATUS_VALUES),
    )
    for field_id, value, allowed in checks:
        if value in allowed:
            continue
        issues.append(
            _system_issue(
                rule_id="ENUM_VALIDATION",
                severity="BLOCK",
                field_id=field_id,
                message=f"{field_id}에 허용되지 않은 상태값이 있습니다.",
                evidence=[],
                suggested_action="허용된 상태값 중 하나로 수정한 뒤 재검증해 주세요.",
                value=value,
                allowed_values=sorted(allowed),
            )
        )
    incoming_validation = document.get("validation")
    if isinstance(incoming_validation, dict):
        value = incoming_validation.get("status")
        if value not in VALIDATION_STATUS_VALUES:
            issues.append(
                _system_issue(
                    rule_id="ENUM_VALIDATION",
                    severity="BLOCK",
                    field_id="validation.status",
                    message="validation.status에 허용되지 않은 상태값이 있습니다.",
                    evidence=[],
                    suggested_action=(
                        "PASS, REVIEW_REQUIRED, BLOCK 중 하나로 수정해 주세요."
                    ),
                    value=value,
                    allowed_values=sorted(VALIDATION_STATUS_VALUES),
                )
            )
    for field_id, field in fields.items():
        if not isinstance(field, dict):
            continue
        value = field.get("information_status")
        if value in INFORMATION_STATUS_VALUES:
            continue
        issues.append(
            _system_issue(
                rule_id="ENUM_VALIDATION",
                severity="BLOCK",
                field_id=str(field_id),
                message="필드에 허용되지 않은 정보 상태값이 있습니다.",
                evidence=list(field.get("evidence") or []),
                suggested_action=(
                    "PRESENT, NONE, NOT_ASSESSED, UNCERTAIN 중 하나로 수정해 주세요."
                ),
                value=value,
                allowed_values=sorted(INFORMATION_STATUS_VALUES),
            )
        )
    return issues


def _field_classification_validation_issues(
    document: dict[str, Any],
) -> list[dict[str, Any]]:
    api2 = document.get("api2") if isinstance(document.get("api2"), dict) else {}
    clinical_record = (
        api2.get("clinical_record")
        if isinstance(api2.get("clinical_record"), dict)
        else {}
    )
    result: list[dict[str, Any]] = []
    for source_issue in field_misclassification_issues(clinical_record):
        field_id = str(source_issue.get("field_id") or "")
        draft_evidence = list(_draft_field(document, field_id).get("evidence") or [])
        source_evidence = source_issue.get("evidence")
        if not draft_evidence and isinstance(source_evidence, dict):
            draft_evidence = [copy.deepcopy(source_evidence)]
        result.append(
            _system_issue(
                rule_id="FIELD_MISCLASSIFICATION",
                severity="REVIEW_REQUIRED",
                field_id=field_id,
                message=str(source_issue.get("message") or "필드 배치를 확인해 주세요."),
                evidence=draft_evidence,
                suggested_action="원문 의미를 확인하고 올바른 기록 항목으로 수정해 주세요.",
                code="FIELD_MISCLASSIFICATION",
                reason=source_issue.get("reason"),
                value=source_issue.get("value"),
            )
        )
    return result


def _normalization_validation_issues(document: dict[str, Any]) -> list[dict[str, Any]]:
    draft = document.get("draft") if isinstance(document.get("draft"), dict) else {}
    review_items = draft.get("review_items")
    if not isinstance(review_items, list):
        return []
    issues: list[dict[str, Any]] = []
    for item in review_items:
        if not isinstance(item, dict) or item.get("type") != "normalization_unsupported":
            continue
        evidence = item.get("evidence")
        issues.append(
            _system_issue(
                rule_id="NORMALIZATION_UNSUPPORTED",
                severity="REVIEW_REQUIRED",
                field_id=str(item.get("field_id") or "workflow"),
                message="승인된 출처가 없는 정규화 제안은 초안에 적용하지 않았습니다.",
                evidence=[copy.deepcopy(evidence)] if isinstance(evidence, dict) else [],
                suggested_action="원문과 제안값을 확인하고 의료진이 직접 선택하거나 수정해 주세요.",
                raw_value=item.get("source"),
                proposed_value=item.get("proposed_value"),
            )
        )
    return issues


def _unresolved_uncertainty_issues(
    document: dict[str, Any],
    fields: dict[str, Any],
) -> list[dict[str, Any]]:
    draft = document.get("draft") if isinstance(document.get("draft"), dict) else {}
    review_items = draft.get("review_items")
    normalization_fields = {
        str(item.get("field_id"))
        for item in review_items or []
        if isinstance(item, dict) and item.get("type") == "normalization_unsupported"
    }
    issues: list[dict[str, Any]] = []
    for field_id, field in fields.items():
        if (
            not isinstance(field, dict)
            or field.get("information_status") != "UNCERTAIN"
            or str(field_id) in normalization_fields
        ):
            continue
        issues.append(
            _system_issue(
                rule_id="UNRESOLVED_UNCERTAINTY",
                severity="REVIEW_REQUIRED",
                field_id=str(field_id),
                message="확정되지 않은 임상정보가 남아 있습니다.",
                evidence=list(field.get("evidence") or []),
                suggested_action="관련 원문과 후보를 확인하고 값을 확정하거나 수정해 주세요.",
                value=field.get("value"),
            )
        )
    return issues


def _enrich_policy_evidence(
    issues: list[dict[str, Any]],
    policy_index_path: Path | str | None,
    policy_evidence_provider: Callable[[str, str], dict[str, Any]] | None,
    workflow_phase: str,
) -> None:
    if workflow_phase not in {"FINALIZATION", "POST_SIGN_EDIT"}:
        for issue in issues:
            issue["policy_evidence"] = []
            issue["policy_evidence_status"] = "not_applicable"
        return
    if policy_evidence_provider is None and policy_index_path is None:
        return
    provider = policy_evidence_provider
    if provider is None:
        provider = lambda rule_id, query: retrieve_policy_evidence(
            rule_id,
            query,
            index_path=policy_index_path,
        )
    for issue in issues:
        if not re.fullmatch(r"G\d{2}", str(issue.get("rule_id") or "")):
            issue["policy_evidence_status"] = "not_applicable"
            continue
        try:
            response = provider(
                issue["rule_id"],
                f"{issue['message']} {issue['suggested_action']}",
            )
            evidence = response.get("results") if isinstance(response, dict) else []
            if not isinstance(evidence, list):
                evidence = []
        except Exception:
            evidence = []
        issue["policy_evidence"] = evidence
        issue["policy_evidence_status"] = (
            "available" if evidence else "unavailable"
        )


def validate_clinical_workflow(
    document: dict[str, Any],
    *,
    guardrail_path: Path | str = DEFAULT_GUARDRAIL_RULES,
    thresholds_path: Path | str = DEFAULT_VALIDATION_THRESHOLDS,
    policy_index_path: Path | str | None = None,
    policy_evidence_provider: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a workflow draft without changing any input or draft value."""

    rules = _rules_by_id(_load_json(guardrail_path))
    thresholds = _load_json(thresholds_path)
    _attach_runtime_thresholds(rules, thresholds)
    issues: list[dict[str, Any]] = []

    if document.get("record_status") == "COMPLETED":
        approval = document.get("clinician_approval")
        approved = isinstance(approval, dict) and approval.get("approved") is True
        if not approved:
            issues.append(
                _issue(
                    rules["G19"],
                    field_id="record_status",
                    message="AI 초안이 의료진 승인 없이 작성 완료로 처리되었습니다.",
                    evidence=[],
                    suggested_action=(
                        "기록 상태를 초안으로 유지하고 의료진 검토와 최종 승인을 "
                        "확인해 주세요."
                    ),
                )
            )

    draft = document.get("draft") if isinstance(document.get("draft"), dict) else {}
    fields = draft.get("fields") if isinstance(draft.get("fields"), dict) else {}
    issues.extend(_enum_issues(document, fields))
    issues.extend(_field_classification_validation_issues(document))
    issues.extend(_normalization_validation_issues(document))
    issues.extend(_unresolved_uncertainty_issues(document, fields))
    if document.get("processing_status") in {"partial", "failed"}:
        issues.append(
            _system_issue(
                rule_id="PROCESSING_STATUS",
                severity="REVIEW_REQUIRED",
                field_id="workflow",
                message="일부 처리 단계가 완료되지 않았습니다.",
                evidence=[],
                suggested_action=(
                    "처리 오류를 확인하고 다시 실행하거나 확보된 초안을 직접 "
                    "검토해 주세요."
                ),
                processing_status=document.get("processing_status"),
            )
        )
    specialized_grounding_fields = {
        "medications",
        "allergy",
        "physical_examination",
        "treatment_plan",
    }
    for field_id, field in fields.items():
        if (
            field_id in specialized_grounding_fields
            or not isinstance(field, dict)
            or field.get("information_status") not in {"PRESENT", "UNCERTAIN"}
            or not _field_has_unsupported_atoms(document, str(field_id))
        ):
            continue
        issues.append(
            _issue(
                rules["G01"],
                field_id=str(field_id),
                message="대화 또는 구조화 데이터로 추적할 수 없는 임상 사실입니다.",
                evidence=list(field.get("evidence") or []),
                suggested_action=(
                    "원문 근거를 확인하고 근거가 없다면 의료진이 초안 값을 "
                    "직접 수정해 주세요."
                ),
            )
        )

    segment_map = _segments(document)
    for field_id, field in fields.items():
        if (
            field_id in {"chief_complaint", "allergy"}
            or not isinstance(field, dict)
            or field.get("information_status") != "NONE"
        ):
            continue
        evidence_texts = _field_evidence_texts(field, segment_map)
        if any(_is_explicit_negation(text) for text in evidence_texts):
            continue
        issues.append(
            _issue(
                rules["G02"],
                field_id=str(field_id),
                message="확인되지 않은 항목이 없음으로 기록되었습니다.",
                evidence=list(field.get("evidence") or []),
                suggested_action=(
                    "명시적인 부정 답변이 있는지 원문을 확인하고, 없으면 "
                    "미확인으로 수정해 주세요."
                ),
            )
        )

    for field_id, field in fields.items():
        if (
            not isinstance(field, dict)
            or field.get("information_status") not in {"PRESENT", "NONE"}
        ):
            continue
        evidence_texts = _field_evidence_texts(field, segment_map)
        if not any(_is_uncertain_text(text) for text in evidence_texts):
            continue
        issues.append(
            _issue(
                rules["G03"],
                field_id=str(field_id),
                message="불확실한 진술이 확정 상태로 기록되었습니다.",
                evidence=list(field.get("evidence") or []),
                suggested_action=(
                    "불확실성 표현과 원문을 확인하고 확인 필요 상태로 수정해 주세요."
                ),
            )
        )

    for field_id, field in fields.items():
        if (
            not isinstance(field, dict)
            or field.get("information_status") == "UNCERTAIN"
        ):
            continue
        evidence_texts = _field_evidence_texts(field, segment_map)
        if not _has_contradictory_evidence(evidence_texts):
            continue
        issues.append(
            _issue(
                rules["G04"],
                field_id=str(field_id),
                message="서로 상충하는 진술 중 하나가 확정적으로 기록되었습니다.",
                evidence=list(field.get("evidence") or []),
                suggested_action=(
                    "상충하는 양쪽 원문을 확인하고 확인 필요 상태로 수정해 주세요."
                ),
            )
        )

    medications = _draft_field(document, "medications")
    if (
        medications.get("information_status") in {"PRESENT", "UNCERTAIN"}
        and _field_has_unsupported_atoms(document, "medications")
    ):
        issues.append(
            _issue(
                rules["G06"],
                field_id="medications",
                message="원문에 없는 약물명 또는 복용 세부정보가 포함되었습니다.",
                evidence=list(medications.get("evidence") or []),
                suggested_action=(
                    "약물명·용량·단위·경로·빈도를 원문이나 구조화된 약물정보와 "
                    "대조해 주세요."
                ),
            )
        )

    allergy = _draft_field(document, "allergy")
    if allergy.get("information_status") == "NONE":
        allergy_evidence = _field_evidence_texts(allergy, segment_map)
        if not any(_is_explicit_negation(text) for text in allergy_evidence):
            issues.append(
                _issue(
                    rules["G07"],
                    field_id="allergy",
                    message="알레르기를 확인하지 않았는데 없음으로 기록되었습니다.",
                    evidence=list(allergy.get("evidence") or []),
                    suggested_action=(
                        "알레르기 질문과 명시적인 부정 답변을 확인하고, 확인되지 "
                        "않았다면 미확인으로 수정해 주세요."
                    ),
                )
            )

    plan = _draft_field(document, "treatment_plan")
    draft = document.get("draft") if isinstance(document.get("draft"), dict) else {}
    treatment_plan_validation_review = any(
        isinstance(item, dict)
        and item.get("type") == "treatment_plan_validation"
        and item.get("field_id") == "treatment_plan"
        and item.get("needs_review") is True
        for item in draft.get("review_items", [])
    )
    if (
        plan.get("information_status") in {"PRESENT", "UNCERTAIN"}
        and (
            treatment_plan_validation_review
            or _field_has_unsupported_atoms(document, "treatment_plan")
        )
    ):
        issues.append(
            _issue(
                rules["G08"],
                field_id="treatment_plan",
                message="의료진 발화에 없는 검사·투약·처치 계획이 포함되었습니다.",
                evidence=list(plan.get("evidence") or []),
                suggested_action=(
                    "계획을 명시한 의료진 발화를 확인하고 근거가 없으면 초안을 "
                    "수정해 주세요."
                ),
            )
        )

    issues.extend(_vital_issues(document))

    physical = _draft_field(document, "physical_examination")
    physical_validation_review = any(
        isinstance(item, dict)
        and item.get("type") == "physical_examination_validation"
        and item.get("field_id") == "physical_examination"
        and item.get("needs_review") is True
        for item in draft.get("review_items", [])
    )
    if (
        physical.get("information_status") == "PRESENT"
        and (
            physical_validation_review
            or _field_has_unsupported_atoms(document, "physical_examination")
        )
    ):
        issues.append(
            _issue(
                rules["G09"],
                field_id="physical_examination",
                message=(
                    "대화나 구조화 데이터에서 확인되지 않은 신체검진 소견이 "
                    "초안에 포함되었습니다."
                ),
                evidence=list(physical.get("evidence") or []),
                suggested_action=(
                    "원문 또는 의료진이 직접 기록한 진찰소견을 확인하고 초안을 "
                    "수정해 주세요."
                ),
            )
        )

    _enrich_policy_evidence(
        issues,
        policy_index_path,
        policy_evidence_provider,
        str(
            document.get("workflow_phase")
            or (
                "FINALIZATION"
                if document.get("record_status") == "COMPLETED"
                else "DRAFT_GENERATION"
            )
        ),
    )
    status = (
        "BLOCK"
        if any(issue["severity"] == "BLOCK" for issue in issues)
        else "REVIEW_REQUIRED"
        if issues
        else "PASS"
    )
    structured_context = document.get("structured_patient_data")
    structured_applicability = (
        "APPLICABLE"
        if isinstance(structured_context, dict) and bool(structured_context)
        else "NOT_APPLICABLE"
    )
    return {
        "status": status,
        "issues": issues,
        "rule_applicability": {
            "G16": structured_applicability,
            "G17": structured_applicability,
            "G18": structured_applicability,
        },
    }
