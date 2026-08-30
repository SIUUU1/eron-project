from __future__ import annotations

from types import SimpleNamespace
import unittest

from clinicalnlp_api3.scispacy_runtime import ScispacyUmlsExtractor


class _Entity:
    def __init__(self, text, start_char, end_char, candidates):
        self.text = text
        self.start_char = start_char
        self.end_char = end_char
        self._ = SimpleNamespace(kb_ents=candidates)


class _Document:
    def __init__(self, entities):
        self.ents = tuple(entities)


class _Pipeline:
    def __init__(self, documents):
        self.documents = documents
        self.inputs = []

    def pipe(self, texts):
        self.inputs = list(texts)
        return iter(self.documents)


class _Linker:
    def __init__(self):
        self.calls = []
        self.kb = SimpleNamespace(
            cui_to_entity={
                "C0013404": SimpleNamespace(
                    canonical_name="Dyspnea",
                    types=("T184",),
                )
            }
        )

    def __call__(self, document):
        self.calls.append(document)
        return document


class ScispacyRuntimeTests(unittest.TestCase):
    def test_full_context_detection_links_only_documents_with_medical_spans(self):
        empty_document = _Document(())
        medical_document = _Document(
            (_Entity("Dyspnea", 0, 7, (("C0013404", 0.97),)),)
        )
        pipeline = _Pipeline((empty_document, medical_document))
        linker = _Linker()
        extractor = object.__new__(ScispacyUmlsExtractor)
        extractor._nlp = pipeline
        extractor._linker = linker
        extractor._metadata = {"threshold": 0.8}

        spans, metadata = extractor.extract(
            [
                {
                    "segment_id": "seg_1",
                    "translated_text_en": "The patient arrived today.",
                },
                {
                    "segment_id": "seg_2",
                    "translated_text_en": "Dyspnea worsened.",
                },
            ]
        )

        self.assertEqual(
            pipeline.inputs,
            ["The patient arrived today.", "Dyspnea worsened."],
        )
        self.assertEqual(linker.calls, [medical_document])
        self.assertEqual(metadata["input_segment_count"], 2)
        self.assertEqual(metadata["detected_span_count"], 1)
        self.assertEqual(metadata["detected_span_character_count"], 7)
        self.assertEqual(metadata["linker_document_count"], 1)
        self.assertEqual(spans[0]["segment_id"], "seg_2")
        self.assertEqual(spans[0]["text"], "Dyspnea")
        self.assertEqual(spans[0]["umls_candidates"][0]["cui"], "C0013404")


if __name__ == "__main__":
    unittest.main()
