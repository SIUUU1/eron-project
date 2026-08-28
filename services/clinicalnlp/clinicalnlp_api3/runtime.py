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

    def generate_draft(self, whisper_payload: dict[str, Any]) -> dict[str, Any]:
        result = run_clinical_workflow(
            whisper_payload,
            retriever=self.retriever,
            clinical_extractor=self.clinical_extractor,
            query_expander=self.query_expander,
            medical_query_resolver=self.medical_query_resolver,
            preserve_unsupported=True,
            include_query_resolution_summary=True,
        )
        return to_clinical_workflow_v2(result)


def create_clinical_runtime(
    *,
    retriever: Any,
    clinical_extractor: Any,
    query_expander: Any | None = None,
    medical_query_resolver: Any | None = None,
) -> ClinicalDraftRuntime:
    """Compose the ClinicalNLP implementation behind its draft interface."""

    return ClinicalDraftRuntime(
        retriever=retriever,
        clinical_extractor=clinical_extractor,
        query_expander=query_expander,
        medical_query_resolver=medical_query_resolver,
    )
