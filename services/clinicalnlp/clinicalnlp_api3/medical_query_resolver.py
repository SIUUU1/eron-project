from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import math
import re
from typing import Literal, final


__all__ = (
    "QUERY_RESOLUTION_SCHEMA_VERSION",
    "QUERY_RESOLUTION_MODES",
    "QUERY_RESOLUTION_STATUSES",
    "QUERY_ISSUE_LANES",
    "LOCAL_DICTIONARY_COLLECTIONS",
    "QueryResolutionMode",
    "QueryResolutionStatus",
    "QueryRoute",
    "EvidenceScope",
    "QueryIssueLane",
    "LocalDictionaryCollection",
    "InvalidMedicalQueryDocumentError",
    "InvalidQueryResolutionError",
    "MedicalQuerySegment",
    "MedicalQueryDocument",
    "QueryTextSpan",
    "CandidateEvidence",
    "LocalDictionaryMatch",
    "UmlsCandidateProvenance",
    "ResolvedCandidate",
    "QueryResolutionIssue",
    "QueryResolutionTelemetry",
    "QueryResolution",
    "MedicalQueryResolver",
)


QUERY_RESOLUTION_SCHEMA_VERSION = "medical-query-resolution-v1"
QUERY_RESOLUTION_MODES = ("legacy", "shadow", "umls_primary")
QUERY_RESOLUTION_STATUSES = ("complete", "partial")
QUERY_ISSUE_LANES = ("baseline", "umls")
LOCAL_DICTIONARY_COLLECTIONS = (
    "drug_terms",
    "procedure_terms",
    "anatomy_terms",
    "emergency_terms",
    "kcd9_terms",
)
_QUERY_ROUTES = (
    "raw_exact",
    "approved_alias",
    "raw_similarity",
    "umls",
    "ngram_fallback",
)
_ROUTE_PRIORITY = {
    route: priority for priority, route in enumerate(_QUERY_ROUTES)
}
_EVIDENCE_SCOPES = ("exact_raw_span", "whole_raw_segment")
_ROUTE_EVIDENCE_SCOPE = {
    "raw_exact": "exact_raw_span",
    "approved_alias": "exact_raw_span",
    "raw_similarity": "exact_raw_span",
    "umls": "whole_raw_segment",
    "ngram_fallback": "whole_raw_segment",
}
_ROUTE_REVIEW_STATUSES = {
    "raw_exact": ("official", "needs_review"),
    "approved_alias": ("approved",),
    "raw_similarity": ("needs_review",),
    "umls": ("needs_review",),
    "ngram_fallback": ("needs_review",),
}
_COLLECTION_ENTITY_PREFIXES = {
    "drug_terms": "drug:",
    "procedure_terms": "procedure:",
    "anatomy_terms": "anatomy:",
    "emergency_terms": "emergency:",
    "kcd9_terms": "kcd:",
}
_ISSUE_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_ISSUE_STAGE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")

QueryResolutionMode = Literal["legacy", "shadow", "umls_primary"]
QueryResolutionStatus = Literal["complete", "partial"]
QueryRoute = Literal[
    "raw_exact",
    "approved_alias",
    "raw_similarity",
    "umls",
    "ngram_fallback",
]
EvidenceScope = Literal["exact_raw_span", "whole_raw_segment"]
QueryIssueLane = Literal["baseline", "umls"]
LocalDictionaryCollection = Literal[
    "drug_terms",
    "procedure_terms",
    "anatomy_terms",
    "emergency_terms",
    "kcd9_terms",
]


class InvalidMedicalQueryDocumentError(ValueError):
    pass


class InvalidQueryResolutionError(ValueError):
    pass


def _valid_segment_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= 128
    )


@dataclass(frozen=True)
class MedicalQuerySegment:
    segment_id: str
    raw_text: str
    translated_text_en: str | None = None
    collection_hints: frozenset[LocalDictionaryCollection] | None = None

    def __post_init__(self) -> None:
        if not _valid_segment_id(self.segment_id):
            raise InvalidMedicalQueryDocumentError(
                "segment_id must be a non-empty, unpadded string"
            )
        if not isinstance(self.raw_text, str) or not self.raw_text:
            raise InvalidMedicalQueryDocumentError(
                "raw_text must be a non-empty string"
            )
        if self.translated_text_en is not None and (
            not isinstance(self.translated_text_en, str)
            or not self.translated_text_en.strip()
        ):
            raise InvalidMedicalQueryDocumentError(
                "translated_text_en must be non-empty when present"
            )
        if self.collection_hints is not None and (
            not isinstance(self.collection_hints, frozenset)
            or not self.collection_hints
            or any(
                collection not in LOCAL_DICTIONARY_COLLECTIONS[:-1]
                for collection in self.collection_hints
            )
        ):
            raise InvalidMedicalQueryDocumentError(
                "collection_hints must contain supported medical vector collections"
            )


@dataclass(frozen=True)
class MedicalQueryDocument:
    segments: tuple[MedicalQuerySegment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.segments, tuple) or any(
            not isinstance(segment, MedicalQuerySegment)
            for segment in self.segments
        ):
            raise InvalidMedicalQueryDocumentError(
                "segments must be a tuple of MedicalQuerySegment values"
            )
        segment_ids = [segment.segment_id for segment in self.segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise InvalidMedicalQueryDocumentError(
                "medical query segment IDs must be unique"
            )


@dataclass(frozen=True)
class QueryTextSpan:
    text: str
    start_char: int
    end_char: int

    def __post_init__(self) -> None:
        if type(self.start_char) is not int or self.start_char < 0:
            raise InvalidQueryResolutionError(
                "start_char must be a non-negative integer"
            )
        if type(self.end_char) is not int or self.end_char <= self.start_char:
            raise InvalidQueryResolutionError(
                "end_char must be greater than start_char"
            )
        if not isinstance(self.text, str) or not self.text or (
            self.end_char - self.start_char != len(self.text)
        ):
            raise InvalidQueryResolutionError(
                "span length must match text"
            )


@dataclass(frozen=True)
class CandidateEvidence:
    scope: EvidenceScope
    raw_span: QueryTextSpan | None = None
    translated_query_span: QueryTextSpan | None = None

    def __post_init__(self) -> None:
        if self.scope not in _EVIDENCE_SCOPES:
            raise InvalidQueryResolutionError("unsupported evidence scope")
        if self.scope == "exact_raw_span" and (
            not isinstance(self.raw_span, QueryTextSpan)
            or self.translated_query_span is not None
        ):
            raise InvalidQueryResolutionError(
                "exact_raw_span evidence requires only raw_span"
            )
        if self.scope == "whole_raw_segment" and (
            not isinstance(self.translated_query_span, QueryTextSpan)
            or self.raw_span is not None
        ):
            raise InvalidQueryResolutionError(
                "whole_raw_segment evidence requires only translated_query_span"
            )


@dataclass(frozen=True)
class LocalDictionaryMatch:
    collection: LocalDictionaryCollection
    entity_id: str
    dictionary_version: str
    canonical_ko: str
    canonical_en: str | None
    retrieval_score: float

    def __post_init__(self) -> None:
        if self.collection not in LOCAL_DICTIONARY_COLLECTIONS:
            raise InvalidQueryResolutionError(
                "candidate collection must be a local dictionary collection"
            )
        expected_prefix = _COLLECTION_ENTITY_PREFIXES[self.collection]
        if (
            not isinstance(self.entity_id, str)
            or not self.entity_id.startswith(expected_prefix)
            or len(self.entity_id) <= len(expected_prefix)
            or len(self.entity_id) > 256
        ):
            raise InvalidQueryResolutionError(
                "candidate entity_id does not match its local collection"
            )
        if (
            not isinstance(self.dictionary_version, str)
            or not self.dictionary_version
            or self.dictionary_version != self.dictionary_version.strip()
            or len(self.dictionary_version) > 128
        ):
            raise InvalidQueryResolutionError(
                "dictionary_version must be a bounded, unpadded string"
            )
        if (
            not isinstance(self.canonical_ko, str)
            or not self.canonical_ko.strip()
            or len(self.canonical_ko) > 512
        ):
            raise InvalidQueryResolutionError(
                "canonical_ko must be a bounded, non-empty string"
            )
        if self.canonical_en is not None and (
            not isinstance(self.canonical_en, str)
            or not self.canonical_en.strip()
            or len(self.canonical_en) > 512
        ):
            raise InvalidQueryResolutionError(
                "canonical_en must be bounded and non-empty when present"
            )
        if (
            isinstance(self.retrieval_score, bool)
            or not isinstance(self.retrieval_score, (int, float))
            or not math.isfinite(self.retrieval_score)
            or not 0.0 <= self.retrieval_score <= 1.0
        ):
            raise InvalidQueryResolutionError(
                "retrieval_score must be a finite number between zero and one"
            )


@dataclass(frozen=True)
class UmlsCandidateProvenance:
    cui: str
    semantic_types: tuple[str, ...]
    linking_score: float

    def __post_init__(self) -> None:
        if (
            not isinstance(self.cui, str)
            or not self.cui.strip()
            or self.cui != self.cui.strip()
            or len(self.cui) > 64
        ):
            raise InvalidQueryResolutionError(
                "UMLS CUI must be a bounded, unpadded string"
            )
        if not isinstance(self.semantic_types, tuple) or any(
            not isinstance(value, str)
            or not re.fullmatch(r"T\d{3}", value)
            for value in self.semantic_types
        ):
            raise InvalidQueryResolutionError(
                "UMLS semantic_types must be a tuple of T-prefixed identifiers"
            )
        if len(self.semantic_types) != len(set(self.semantic_types)):
            raise InvalidQueryResolutionError(
                "UMLS semantic_types must be unique"
            )
        if (
            isinstance(self.linking_score, bool)
            or not isinstance(self.linking_score, (int, float))
            or not math.isfinite(self.linking_score)
            or not 0.0 <= self.linking_score <= 1.0
        ):
            raise InvalidQueryResolutionError(
                "UMLS linking_score must be between zero and one"
            )


@dataclass(frozen=True)
class ResolvedCandidate:
    segment_id: str
    route: QueryRoute
    review_status: str
    dictionary_match: LocalDictionaryMatch
    evidence: CandidateEvidence | None = None
    umls_provenance: UmlsCandidateProvenance | None = None

    def __post_init__(self) -> None:
        if not _valid_segment_id(self.segment_id):
            raise InvalidQueryResolutionError(
                "candidate segment_id must be a non-empty, unpadded string"
            )
        if not isinstance(self.evidence, CandidateEvidence):
            raise InvalidQueryResolutionError("candidate evidence is required")
        if self.route not in _QUERY_ROUTES:
            raise InvalidQueryResolutionError("unsupported candidate route")
        expected_scope = _ROUTE_EVIDENCE_SCOPE[self.route]
        if self.evidence.scope != expected_scope:
            raise InvalidQueryResolutionError(
                f"{self.route} candidates require {expected_scope} evidence"
            )
        allowed_review_statuses = _ROUTE_REVIEW_STATUSES[self.route]
        if self.review_status not in allowed_review_statuses:
            raise InvalidQueryResolutionError(
                f"unsupported review status for {self.route} candidates"
            )
        if not isinstance(self.dictionary_match, LocalDictionaryMatch):
            raise InvalidQueryResolutionError(
                "candidate must reference a typed local dictionary match"
            )
        if self.umls_provenance is not None and (
            self.route != "umls"
            or not isinstance(self.umls_provenance, UmlsCandidateProvenance)
        ):
            raise InvalidQueryResolutionError(
                "UMLS provenance is allowed only on UMLS candidates"
            )


@dataclass(frozen=True)
class QueryResolutionIssue:
    code: str
    stage: str
    lane: QueryIssueLane
    segment_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not _ISSUE_CODE_RE.fullmatch(
            self.code
        ):
            raise InvalidQueryResolutionError(
                "issue code must be a bounded uppercase identifier"
            )
        if not isinstance(self.stage, str) or not _ISSUE_STAGE_RE.fullmatch(
            self.stage
        ):
            raise InvalidQueryResolutionError(
                "issue stage must be a bounded lowercase identifier"
            )
        if self.lane not in QUERY_ISSUE_LANES:
            raise InvalidQueryResolutionError(
                "issue lane must be baseline or umls"
            )
        if self.segment_id is not None and not _valid_segment_id(
            self.segment_id
        ):
            raise InvalidQueryResolutionError(
                "issue segment_id must be a non-empty, unpadded string"
            )


@dataclass(frozen=True)
class QueryResolutionTelemetry:
    umls_ms: float = 0.0
    umls_model_load_ms: float = 0.0
    umls_mention_detection_ms: float = 0.0
    umls_linking_ms: float = 0.0
    umls_extraction_ms: float = 0.0
    umls_worker_overhead_ms: float = 0.0
    umls_worker_cold_start_overhead_ms: float = 0.0
    umls_worker_batch_count: int = 0
    umls_worker_fallback_batch_count: int = 0
    umls_worker_cold_start_batch_count: int = 0
    umls_input_segment_count: int = 0
    umls_input_character_count: int = 0
    umls_detected_span_count: int = 0
    umls_detected_span_character_count: int = 0
    umls_linker_document_count: int = 0
    dictionary_ms: float = 0.0
    vector_ms: float = 0.0
    exact_statement_count: int = 0
    vector_statement_count: int = 0
    search_cache_hit_count: int = 0
    routed_query_count: int = 0
    routing_conflict_count: int = 0
    exact_search_batch_count: int = 0
    exact_search_query_count: int = 0
    exact_search_hit_count: int = 0
    vector_fallback_batch_count: int = 0
    vector_fallback_query_count: int = 0
    vector_fallback_hit_count: int = 0
    vector_fallback_empty_count: int = 0
    umls_surface_query_count: int = 0
    umls_canonical_query_count: int = 0
    semantic_fallback_query_count: int = 0
    ngram_fallback_query_count: int = 0
    vector_collection_ms: tuple[tuple[str, float], ...] = ()
    vector_collection_statement_counts: tuple[tuple[str, int], ...] = ()
    vector_collection_batch_counts: tuple[tuple[str, int], ...] = ()
    vector_collection_query_counts: tuple[tuple[str, int], ...] = ()
    vector_collection_candidate_counts: tuple[tuple[str, int], ...] = ()
    vector_collection_empty_query_counts: tuple[tuple[str, int], ...] = ()
    vector_partition_ms: tuple[tuple[str, str, float], ...] = ()
    vector_partition_result_counts: tuple[tuple[str, str, int], ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("umls_ms", self.umls_ms),
            ("umls_model_load_ms", self.umls_model_load_ms),
            ("umls_mention_detection_ms", self.umls_mention_detection_ms),
            ("umls_linking_ms", self.umls_linking_ms),
            ("umls_extraction_ms", self.umls_extraction_ms),
            ("umls_worker_overhead_ms", self.umls_worker_overhead_ms),
            (
                "umls_worker_cold_start_overhead_ms",
                self.umls_worker_cold_start_overhead_ms,
            ),
            ("dictionary_ms", self.dictionary_ms),
            ("vector_ms", self.vector_ms),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise InvalidQueryResolutionError(
                    f"{name} must be a finite non-negative number"
                )
        for name, value in (
            ("umls_worker_batch_count", self.umls_worker_batch_count),
            (
                "umls_worker_fallback_batch_count",
                self.umls_worker_fallback_batch_count,
            ),
            (
                "umls_worker_cold_start_batch_count",
                self.umls_worker_cold_start_batch_count,
            ),
            ("umls_input_segment_count", self.umls_input_segment_count),
            ("umls_input_character_count", self.umls_input_character_count),
            ("umls_detected_span_count", self.umls_detected_span_count),
            (
                "umls_detected_span_character_count",
                self.umls_detected_span_character_count,
            ),
            ("umls_linker_document_count", self.umls_linker_document_count),
            ("exact_statement_count", self.exact_statement_count),
            ("vector_statement_count", self.vector_statement_count),
            ("search_cache_hit_count", self.search_cache_hit_count),
            ("routed_query_count", self.routed_query_count),
            ("routing_conflict_count", self.routing_conflict_count),
            ("exact_search_batch_count", self.exact_search_batch_count),
            ("exact_search_query_count", self.exact_search_query_count),
            ("exact_search_hit_count", self.exact_search_hit_count),
            ("vector_fallback_batch_count", self.vector_fallback_batch_count),
            ("vector_fallback_query_count", self.vector_fallback_query_count),
            ("vector_fallback_hit_count", self.vector_fallback_hit_count),
            ("vector_fallback_empty_count", self.vector_fallback_empty_count),
            ("umls_surface_query_count", self.umls_surface_query_count),
            ("umls_canonical_query_count", self.umls_canonical_query_count),
            ("semantic_fallback_query_count", self.semantic_fallback_query_count),
            ("ngram_fallback_query_count", self.ngram_fallback_query_count),
        ):
            if type(value) is not int or value < 0:
                raise InvalidQueryResolutionError(
                    f"{name} must be a non-negative integer"
                )
        vector_collections = frozenset(LOCAL_DICTIONARY_COLLECTIONS[:-1])
        seen_ms: set[str] = set()
        for collection, value in self.vector_collection_ms:
            if collection not in vector_collections or collection in seen_ms:
                raise InvalidQueryResolutionError(
                    "vector_collection_ms must contain unique vector collections"
                )
            seen_ms.add(collection)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise InvalidQueryResolutionError(
                    "vector collection time must be a finite non-negative number"
                )
        seen_counts: set[str] = set()
        for collection, value in self.vector_collection_statement_counts:
            if collection not in vector_collections or collection in seen_counts:
                raise InvalidQueryResolutionError(
                    "vector_collection_statement_counts must contain unique vector collections"
                )
            seen_counts.add(collection)
            if type(value) is not int or value < 0:
                raise InvalidQueryResolutionError(
                    "vector collection statement count must be a non-negative integer"
                )
        for name, values in (
            ("vector_collection_batch_counts", self.vector_collection_batch_counts),
            ("vector_collection_query_counts", self.vector_collection_query_counts),
            ("vector_collection_candidate_counts", self.vector_collection_candidate_counts),
            ("vector_collection_empty_query_counts", self.vector_collection_empty_query_counts),
        ):
            seen: set[str] = set()
            for collection, value in values:
                if collection not in vector_collections or collection in seen:
                    raise InvalidQueryResolutionError(
                        f"{name} must contain unique vector collections"
                    )
                seen.add(collection)
                if type(value) is not int or value < 0:
                    raise InvalidQueryResolutionError(
                        f"{name} values must be non-negative integers"
                    )
        allowed_partitions = frozenset(
            {
                ("drug_terms", "ingredient"),
                ("drug_terms", "product"),
                ("procedure_terms", "all"),
                ("anatomy_terms", "all"),
                ("emergency_terms", "all"),
            }
        )
        seen_partition_ms: set[tuple[str, str]] = set()
        for collection, partition, value in self.vector_partition_ms:
            key = (collection, partition)
            if key not in allowed_partitions or key in seen_partition_ms:
                raise InvalidQueryResolutionError(
                    "vector_partition_ms must contain unique supported partitions"
                )
            seen_partition_ms.add(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise InvalidQueryResolutionError(
                    "vector partition time must be a finite non-negative number"
                )
        seen_partition_counts: set[tuple[str, str]] = set()
        for collection, partition, value in self.vector_partition_result_counts:
            key = (collection, partition)
            if key not in allowed_partitions or key in seen_partition_counts:
                raise InvalidQueryResolutionError(
                    "vector_partition_result_counts must contain unique supported partitions"
                )
            seen_partition_counts.add(key)
            if type(value) is not int or value < 0:
                raise InvalidQueryResolutionError(
                    "vector partition result count must be a non-negative integer"
                )


@dataclass(frozen=True)
class QueryResolution:
    mode: QueryResolutionMode
    status: QueryResolutionStatus
    policy_version: str
    umls_query_count: int = 0
    ngram_query_count: int = 0
    unresolved_count: int = 0
    candidates: tuple[ResolvedCandidate, ...] = ()
    issues: tuple[QueryResolutionIssue, ...] = ()
    telemetry: QueryResolutionTelemetry = field(
        default_factory=QueryResolutionTelemetry,
        repr=False,
        compare=False,
    )
    fallback_used: bool = field(default=False, init=False)
    raw_exact_count: int = field(default=0, init=False)
    schema_version: str = field(
        default=QUERY_RESOLUTION_SCHEMA_VERSION,
        init=False,
    )

    def __post_init__(self) -> None:
        if self.mode not in QUERY_RESOLUTION_MODES:
            raise InvalidQueryResolutionError(
                "unsupported query resolution mode"
            )
        if self.status not in QUERY_RESOLUTION_STATUSES:
            raise InvalidQueryResolutionError(
                "unsupported query resolution status"
            )
        if (
            not isinstance(self.policy_version, str)
            or not self.policy_version.strip()
            or len(self.policy_version) > 128
        ):
            raise InvalidQueryResolutionError(
                "policy_version must be a non-empty string of at most 128 characters"
            )
        counts = (
            self.umls_query_count,
            self.ngram_query_count,
            self.unresolved_count,
        )
        if any(type(value) is not int or value < 0 for value in counts):
            raise InvalidQueryResolutionError(
                "summary counts must be non-negative integers"
            )
        if not isinstance(self.candidates, tuple) or any(
            not isinstance(candidate, ResolvedCandidate)
            for candidate in self.candidates
        ):
            raise InvalidQueryResolutionError(
                "candidates must be a tuple of ResolvedCandidate values"
            )
        if not isinstance(self.issues, tuple) or any(
            not isinstance(issue, QueryResolutionIssue)
            for issue in self.issues
        ):
            raise InvalidQueryResolutionError(
                "issues must be a tuple of QueryResolutionIssue values"
            )
        if not isinstance(self.telemetry, QueryResolutionTelemetry):
            raise InvalidQueryResolutionError(
                "telemetry must be a QueryResolutionTelemetry value"
            )
        if self.mode in {"legacy", "shadow"} and any(
            candidate.route == "umls" for candidate in self.candidates
        ):
            raise InvalidQueryResolutionError(
                "legacy and shadow modes cannot publish UMLS candidates"
            )
        if self.mode == "legacy" and self.umls_query_count:
            raise InvalidQueryResolutionError(
                "legacy mode cannot report UMLS query telemetry"
            )
        if self.mode in {"legacy", "shadow"} and any(
            issue.lane == "umls" for issue in self.issues
        ):
            raise InvalidQueryResolutionError(
                "legacy and shadow UMLS issues belong only in local telemetry"
            )
        if (self.status == "partial") != bool(self.issues):
            raise InvalidQueryResolutionError(
                "partial status and public issues must appear together"
            )
        object.__setattr__(
            self,
            "fallback_used",
            self.mode == "umls_primary" and self.ngram_query_count > 0,
        )
        object.__setattr__(
            self,
            "raw_exact_count",
            sum(candidate.route == "raw_exact" for candidate in self.candidates),
        )

    def validate_against(
        self,
        document: MedicalQueryDocument,
        /,
    ) -> QueryResolution:
        """Reject output that cannot be traced to the immutable input."""
        if not isinstance(document, MedicalQueryDocument):
            raise InvalidMedicalQueryDocumentError(
                "resolver input must be a MedicalQueryDocument"
            )
        segments_by_id = {
            segment.segment_id: segment for segment in document.segments
        }
        for candidate in self.candidates:
            segment = segments_by_id.get(candidate.segment_id)
            if segment is None:
                raise InvalidQueryResolutionError(
                    "candidate references an unknown segment"
                )
            evidence = candidate.evidence
            if evidence.scope == "exact_raw_span":
                span = evidence.raw_span
                if span is None:
                    raise InvalidQueryResolutionError(
                        "exact RAW evidence is missing its span"
                    )
                source = segment.raw_text
            else:
                span = evidence.translated_query_span
                if span is None:
                    raise InvalidQueryResolutionError(
                        "translated evidence is missing its query span"
                    )
                source = segment.translated_text_en
                if source is None:
                    raise InvalidQueryResolutionError(
                        "translated evidence requires a segment translation"
                    )
            if span.end_char > len(source) or (
                source[span.start_char : span.end_char] != span.text
            ):
                raise InvalidQueryResolutionError(
                    "candidate evidence does not match its source segment"
                )
        for issue in self.issues:
            if (
                issue.segment_id is not None
                and issue.segment_id not in segments_by_id
            ):
                raise InvalidQueryResolutionError(
                    "issue references an unknown segment"
                )
        segment_priority = {
            segment.segment_id: priority
            for priority, segment in enumerate(document.segments)
        }

        def candidate_order(candidate: ResolvedCandidate) -> tuple[object, ...]:
            evidence = candidate.evidence
            span = evidence.raw_span or evidence.translated_query_span
            if span is None:
                raise InvalidQueryResolutionError(
                    "candidate evidence is missing its ordered span"
                )
            match = candidate.dictionary_match
            return (
                segment_priority[candidate.segment_id],
                _ROUTE_PRIORITY[candidate.route],
                span.start_char,
                span.end_char,
                span.text,
                -match.retrieval_score,
                match.collection,
                match.entity_id,
                candidate.review_status,
                match.dictionary_version,
                match.canonical_ko,
                match.canonical_en or "",
            )

        if self.candidates != tuple(sorted(self.candidates, key=candidate_order)):
            raise InvalidQueryResolutionError(
                "candidates must follow the deterministic contract order"
            )
        return self


class MedicalQueryResolver(ABC):
    """Validated base contract for one medical query resolver implementation."""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.resolve is not MedicalQueryResolver.resolve:
            raise TypeError(
                "resolver implementations must override _resolve, not resolve"
            )

    @final
    def resolve(self, document: MedicalQueryDocument, /) -> QueryResolution:
        if not isinstance(document, MedicalQueryDocument):
            raise InvalidMedicalQueryDocumentError(
                "resolver input must be a MedicalQueryDocument"
            )
        resolution = self._resolve(document)
        if not isinstance(resolution, QueryResolution):
            raise InvalidQueryResolutionError(
                "resolver output must be a QueryResolution"
            )
        return resolution.validate_against(document)

    @abstractmethod
    def _resolve(self, document: MedicalQueryDocument, /) -> QueryResolution:
        """Implement retrieval without mutating the supplied document."""

