from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .workflow import run_clinical_workflow
from .workflow_contract_v2 import to_clinical_workflow_v2


@dataclass(frozen=True)
class ClinicalDraftRuntime:
    """Generate one reviewable v2 draft without persisting or finalizing it."""

    retriever: Any
    clinical_extractor: Any
    query_expander: Any | None = None
    medical_query_resolver: Any | None = None
    policy_evidence_provider: Any | None = None
    compact_v3_mode: str = "off"

    def generate_draft(self, whisper_payload: dict[str, Any]) -> dict[str, Any]:
        result = run_clinical_workflow(
            whisper_payload,
            retriever=self.retriever,
            clinical_extractor=self.clinical_extractor,
            query_expander=self.query_expander,
            medical_query_resolver=self.medical_query_resolver,
            preserve_unsupported=True,
            include_query_resolution_summary=True,
            compact_v3_mode=self.compact_v3_mode,
        )
        return to_clinical_workflow_v2(
            result,
            policy_evidence_provider=self.policy_evidence_provider,
        )


def create_clinical_runtime(
    *,
    retriever: Any,
    clinical_extractor: Any,
    query_expander: Any | None = None,
    medical_query_resolver: Any | None = None,
    policy_evidence_provider: Any | None = None,
    compact_v3_mode: str = "off",
    compact_v3_compare: bool | None = None,
) -> ClinicalDraftRuntime:
    """Compose the ClinicalNLP implementation behind its draft interface."""

    mode = str(compact_v3_mode or "off").strip().casefold()
    if compact_v3_compare is not None and mode == "off":
        mode = "compare" if compact_v3_compare else "off"
    if mode not in {"off", "compare", "primary"}:
        raise ValueError("compact_v3_mode must be off, compare, or primary")
    return ClinicalDraftRuntime(
        retriever=retriever,
        clinical_extractor=clinical_extractor,
        query_expander=query_expander,
        medical_query_resolver=medical_query_resolver,
        policy_evidence_provider=policy_evidence_provider,
        compact_v3_mode=mode,
    )
