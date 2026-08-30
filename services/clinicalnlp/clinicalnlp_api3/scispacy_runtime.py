from __future__ import annotations

import time
from pathlib import Path
from typing import Any


ALLOWED_UMLS_SEMANTIC_TYPES = {
    # Symptoms, findings, clinical attributes and test results.
    "T033", "T034", "T184", "T201",
    # Diseases, abnormalities, injuries and pathologic functions.
    "T019", "T020", "T037", "T046", "T047", "T048", "T049", "T190", "T191",
    # Anatomy and body substances.
    "T017", "T018", "T021", "T022", "T023", "T024", "T029", "T030", "T031",
    # Clinical procedures.
    "T058", "T059", "T060", "T061", "T063",
    # Drugs, ingredients, allergens and diagnostic substances.
    "T103", "T109", "T116", "T121", "T123", "T125", "T126", "T127", "T129",
    "T130", "T131", "T195", "T200",
    # Medical and drug-delivery devices.
    "T074", "T203",
}


class ScispacyUmlsExtractor:
    """Load the local mention detector and UMLS linker once per worker process."""

    def __init__(
        self,
        *,
        cache_root: Path,
        threshold: float = 0.8,
        max_entities: int = 3,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if not 1 <= max_entities <= 10:
            raise ValueError("max_entities must be between 1 and 10")

        import spacy
        import scispacy
        from scispacy.candidate_generation import CandidateGenerator, LinkerPaths
        from scispacy.linking import EntityLinker  # noqa: F401 - registers pipe
        from scispacy.linking_utils import UmlsKnowledgeBase

        def cached(suffix: str) -> Path:
            matches = [
                path
                for path in (cache_root / "datasets").glob(f"*{suffix}")
                if path.is_file() and not path.name.endswith(f"{suffix}.json")
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    f"expected one cached scispaCy artifact *{suffix}, found {len(matches)}"
                )
            return matches[0]

        load_started = time.perf_counter()
        self._nlp = spacy.load("en_core_sci_sm")
        linker_paths = LinkerPaths(
            ann_index=str(cached(".nmslib_index.bin")),
            tfidf_vectorizer=str(cached(".tfidf_vectorizer.joblib")),
            tfidf_vectors=str(cached(".tfidf_vectors_sparse.npz")),
            concept_aliases_list=str(cached(".concept_aliases.json")),
        )
        kb = UmlsKnowledgeBase(
            file_path=cached(".umls_2022_ab_cat0129.jsonl"),
            types_file_path=str(cached(".umls_semantic_type_tree.tsv")),
        )
        candidate_generator = CandidateGenerator(
            ann_index=linker_paths.get_ann_index(),
            tfidf_vectorizer=linker_paths.get_tfidf_vectorizer(),
            ann_concept_aliases_list=linker_paths.get_concept_aliases(),
            kb=kb,
        )
        self._linker = EntityLinker(
            candidate_generator=candidate_generator,
            resolve_abbreviations=False,
            threshold=threshold,
            filter_for_definitions=False,
            max_entities_per_mention=max_entities,
        )
        model_version = self._nlp.meta.get("version", "unknown")
        self._metadata = {
            "name": "scispacy_umls",
            "scispacy_version": scispacy.__version__,
            "spacy_version": spacy.__version__,
            "mention_model": f"en_core_sci_sm-{model_version}",
            "umls_snapshot": "2022AB",
            "threshold": threshold,
            "max_entities_per_mention": max_entities,
            "filter_for_definitions": False,
            "semantic_type_filter": sorted(ALLOWED_UMLS_SEMANTIC_TYPES),
            "load_latency_ms": round(
                (time.perf_counter() - load_started) * 1000,
                3,
            ),
            "runtime_network_required": False,
        }

    @property
    def metadata(self) -> dict[str, Any]:
        return dict(self._metadata)

    def extract(
        self,
        segments: list[dict[str, str]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        extraction_started = time.perf_counter()
        spans: list[dict[str, Any]] = []
        texts = [segment["translated_text_en"] for segment in segments]
        detection_started = time.perf_counter()
        documents = list(self._nlp.pipe(texts))
        mention_detection_ms = (time.perf_counter() - detection_started) * 1000
        detected_span_count = 0
        detected_span_characters = 0
        linker_document_count = 0
        linking_started = time.perf_counter()
        for segment, doc in zip(segments, documents):
            # Mention detection keeps the full sentence context. The expensive
            # UMLS candidate linker is skipped for sentences with no detected
            # medical spans and otherwise operates only on doc.ents.
            detected_entities = tuple(doc.ents)
            detected_span_count += len(detected_entities)
            detected_span_characters += sum(
                max(0, entity.end_char - entity.start_char)
                for entity in detected_entities
            )
            if not detected_entities:
                continue
            linker_document_count += 1
            doc = self._linker(doc)
            for entity in doc.ents:
                candidates: list[dict[str, Any]] = []
                for cui, score in entity._.kb_ents:
                    concept = self._linker.kb.cui_to_entity.get(cui)
                    if concept is None:
                        continue
                    semantic_types = list(concept.types)
                    if not ALLOWED_UMLS_SEMANTIC_TYPES.intersection(semantic_types):
                        continue
                    candidates.append(
                        {
                            "cui": cui,
                            "canonical_name": concept.canonical_name,
                            "semantic_types": semantic_types,
                            "linking_score": float(score),
                        }
                    )
                spans.append(
                    {
                        "segment_id": segment["segment_id"],
                        "text": entity.text,
                        "start_char": entity.start_char,
                        "end_char": entity.end_char,
                        "umls_candidates": candidates,
                    }
                )
        linking_ms = (time.perf_counter() - linking_started) * 1000
        input_characters = sum(len(text) for text in texts)
        return spans, {
            **self._metadata,
            "input_segment_count": len(segments),
            "input_character_count": input_characters,
            "detected_span_count": detected_span_count,
            "detected_span_character_count": detected_span_characters,
            "linker_document_count": linker_document_count,
            "mention_detection_latency_ms": round(mention_detection_ms, 3),
            "linking_latency_ms": round(linking_ms, 3),
            "extraction_latency_ms": round(
                (time.perf_counter() - extraction_started) * 1000,
                3,
            ),
        }

