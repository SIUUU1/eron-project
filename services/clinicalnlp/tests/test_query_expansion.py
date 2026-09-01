import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import Request, urlopen

from clinicalnlp_api3.pipeline import run_api3
from clinicalnlp_api3.query_expansion import LlamaServerMedicalQueryExpander
from clinicalnlp_api3.workflow import run_clinical_workflow


class MedicalQueryExpansionBoundaryTests(unittest.TestCase):
    def test_compact_translation_returns_each_coordinated_medical_term_for_search(self):
        raw_text = "4일 전부터 코프, 스프텀, 디스프니아가 증가했습니다."

        class TranslationClient:
            def generate_json(
                self,
                *,
                system_prompt,
                user_payload,
                response_format,
                output_label,
            ):
                del system_prompt, user_payload, response_format, output_label
                return {
                    "translations": {
                        "t0001": {
                            "translated_text_en": (
                                "Cough, sputum production, and dyspnea have "
                                "increased over the past 4 days."
                            ),
                            "medical_terms": [
                                {
                                    "source_text": "코프",
                                    "search_terms_en": ["cough"],
                                    "term_type": "symptom_or_sign",
                                },
                                {
                                    "source_text": "스프텀",
                                    "search_terms_en": ["sputum production"],
                                    "term_type": "symptom_or_sign",
                                },
                                {
                                    "source_text": "디스프니아",
                                    "search_terms_en": ["dyspnea"],
                                    "term_type": "symptom_or_sign",
                                },
                            ],
                        }
                    }
                }

        result = LlamaServerMedicalQueryExpander(
            "http://unused.local",
            llm_client=TranslationClient(),
        ).expand([
            {"id": "seg_0001", "start": 0.0, "end": 4.0, "text": raw_text}
        ])

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["_telemetry"]["translation_calls"], 1)
        self.assertEqual(
            [item["search_terms_en"][0] for item in result["items"]],
            ["cough", "sputum production", "dyspnea"],
        )
        self.assertEqual(
            [item["source_span"]["text"] for item in result["items"]],
            ["코프", "스프텀", "디스프니아"],
        )

    def test_environment_factory_uses_ollama_cloud_for_translation(self):
        captured = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_POST(self):
                length = int(self.headers["content-length"])
                captured["path"] = self.path
                captured["authorization"] = self.headers.get("authorization")
                captured["payload"] = json.loads(self.rfile.read(length))
                body = json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "translations": {
                                        "t0": {
                                            "translated_text_en": (
                                                "Shortness of breath"
                                            ),
                                            "medical_terms": [],
                                        }
                                    }
                                }
                            ),
                        },
                        "done": True,
                        "done_reason": "stop",
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(
                "os.environ",
                {
                    "CLINICAL_LLM_PROVIDER": "ollama_cloud",
                    "OLLAMA_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "OLLAMA_MODEL": "gemma4:31b",
                    "OLLAMA_API_KEY": "test-secret",
                },
                clear=False,
            ):
                expander = LlamaServerMedicalQueryExpander.from_environment()
                result = expander._request_compact_translation(
                    context_segments=[{"id": "seg_0001", "text": "숨이 차요"}],
                    target_segment_ids=["t0"],
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(
            result,
            {
                "t0": {
                    "translated_text_en": "Shortness of breath",
                    "medical_terms": [],
                }
            },
        )
        self.assertEqual(captured["path"], "/api/chat")
        self.assertEqual(captured["authorization"], "Bearer test-secret")
        self.assertFalse(captured["payload"]["think"])

    def _serve(self, model_result, *, finish_reason="stop"):
        captured = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_POST(self):
                captured["path"] = self.path
                length = int(self.headers["content-length"])
                captured["payload"] = json.loads(self.rfile.read(length))
                body = json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": finish_reason,
                                "message": {
                                    "content": json.dumps(
                                        model_result, ensure_ascii=False
                                    )
                                },
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, captured

    def _serve_compact_translations(self, translations):
        captured = {"payloads": []}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_POST(self):
                length = int(self.headers["content-length"])
                payload = json.loads(self.rfile.read(length))
                captured["payloads"].append(payload)
                supplied = json.loads(payload["messages"][1]["content"])
                target_ids = supplied["target_segment_ids"]
                body = json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "translations": {
                                                target_id: translations[target_id]
                                                for target_id in target_ids
                                            }
                                        },
                                        ensure_ascii=False,
                                    )
                                },
                            }
                        ]
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, captured

    def _serve_compact_translations_with_batch_failure(
        self, translations, *, failed_ids=None
    ):
        captured = {"target_batches": []}
        failed_ids = set(failed_ids or [])

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_POST(self):
                length = int(self.headers["content-length"])
                payload = json.loads(self.rfile.read(length))
                supplied = json.loads(payload["messages"][1]["content"])
                target_ids = supplied["target_segment_ids"]
                captured["target_batches"].append(target_ids)
                model_result = (
                    {"unexpected": []}
                    if len(target_ids) > 1 or target_ids[0] in failed_ids
                    else {"translations": {
                        target_ids[0]: translations[target_ids[0]]
                    }}
                )
                body = json.dumps({
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(model_result, ensure_ascii=False)
                        },
                    }]
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, captured

    def test_compact_translation_uses_one_request_when_transcript_fits_budget(self):
        segments = [
            {
                "id": f"seg_{index:04d}",
                "start": float(index),
                "end": float(index + 1),
                "text": f"원문 {index}",
            }
            for index in range(1, 6)
        ]
        translations = {
            f"t{index:04d}": f"English translation {index}."
            for index in range(1, 6)
        }
        server, thread, captured = self._serve_compact_translations(translations)
        try:
            result = LlamaServerMedicalQueryExpander(
                f"http://127.0.0.1:{server.server_port}",
                context_size=8192,
            ).expand(segments)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result["status"], "available")
        self.assertEqual(
            result["translated_segments"],
            [
                {
                    "segment_id": f"seg_{index:04d}",
                    "translated_text_en": f"English translation {index}.",
                }
                for index in range(1, 6)
            ],
        )
        self.assertEqual(result["items"], [])
        self.assertEqual(len(captured["payloads"]), 1)
        self.assertEqual(result["_telemetry"]["translation_calls"], 1)
        self.assertGreaterEqual(result["_telemetry"]["translation_ms"], 0)
        for payload in captured["payloads"]:
            supplied = json.loads(payload["messages"][1]["content"])
            self.assertEqual(len(supplied["target_segment_ids"]), 5)
            self.assertEqual(len(supplied["context_segments"]), 5)
            schema = payload["response_format"]["json_schema"]["schema"]
            translation_schema = schema["properties"]["translations"]
            self.assertFalse(translation_schema["additionalProperties"])
            self.assertEqual(
                set(translation_schema["required"]),
                set(supplied["target_segment_ids"]),
            )
            self.assertIn("medical_terms", json.dumps(schema))

    def test_compact_translation_splits_only_when_token_budget_requires_it(self):
        segments = [
            {
                "id": f"seg_{index:04d}",
                "start": float(index),
                "end": float(index + 1),
                "text": (f"긴 임상 문장 {index} " * 80).strip(),
            }
            for index in range(1, 9)
        ]
        translations = {
            f"t{index:04d}": f"English translation {index}."
            for index in range(1, 9)
        }
        server, thread, captured = self._serve_compact_translations(translations)
        try:
            result = LlamaServerMedicalQueryExpander(
                f"http://127.0.0.1:{server.server_port}",
                context_size=2048,
                max_output_tokens=1024,
            ).expand(segments)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result["status"], "available")
        self.assertEqual(len(result["translated_segments"]), 8)
        self.assertGreater(result["_telemetry"]["translation_calls"], 1)
        requested_ids = [
            target_id
            for payload in captured["payloads"]
            for target_id in json.loads(payload["messages"][1]["content"])[
                "target_segment_ids"
            ]
        ]
        self.assertEqual(
            requested_ids,
            [f"t{index:04d}" for index in range(1, 9)],
        )

    def test_invalid_batch_retries_by_bisection_without_losing_results(self):
        segments = [
            {
                "id": f"seg_{index:04d}",
                "start": float(index),
                "end": float(index + 1),
                "text": f"원문 {index}",
            }
            for index in range(1, 4)
        ]
        translations = {
            f"t{index:04d}": f"English translation {index}."
            for index in range(1, 4)
        }
        server, thread, captured = (
            self._serve_compact_translations_with_batch_failure(translations)
        )
        try:
            result = LlamaServerMedicalQueryExpander(
                f"http://127.0.0.1:{server.server_port}",
            ).expand(segments)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result["status"], "available")
        self.assertFalse(result["fallback_used"])
        self.assertEqual(len(result["translated_segments"]), 3)
        self.assertEqual(
            captured["target_batches"],
            [
                ["t0001", "t0002", "t0003"],
                ["t0001"],
                ["t0002", "t0003"],
                ["t0002"],
                ["t0003"],
            ],
        )
        self.assertEqual(result["_telemetry"]["translation_calls"], 5)

    def test_single_segment_failure_preserves_other_batch_translations(self):
        segments = [
            {
                "id": f"seg_{index:04d}",
                "start": float(index),
                "end": float(index + 1),
                "text": f"원문 {index}",
            }
            for index in range(1, 4)
        ]
        translations = {
            f"t{index:04d}": f"English translation {index}."
            for index in range(1, 4)
        }
        server, thread, _ = self._serve_compact_translations_with_batch_failure(
            translations,
            failed_ids={"t0002"},
        )
        try:
            result = LlamaServerMedicalQueryExpander(
                f"http://127.0.0.1:{server.server_port}",
            ).expand(segments)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result["status"], "available")
        self.assertTrue(result["fallback_used"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["failed_segment_ids"], ["seg_0002"])
        self.assertEqual(
            [item["segment_id"] for item in result["translated_segments"]],
            ["seg_0001", "seg_0003"],
        )
        self.assertEqual(result["error_code"], "PartialTranslationFailure")

    def test_translates_each_whole_segment_with_compact_output(self):
        text = "코프와 스프텀이 증가했습니다. 오늘 날씨는 맑습니다."
        translated_text = (
            "Cough and sputum have increased. The weather is clear today."
        )
        server, thread, captured = self._serve_compact_translations({
            "t0001": translated_text,
        })
        try:
            result = LlamaServerMedicalQueryExpander(
                f"http://127.0.0.1:{server.server_port}",
                model_name="same-local-gemma",
            ).expand([
                {"id": "seg_0001", "start": 0.0, "end": 2.0, "text": text}
            ])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["method"], "GEMMA_FULL_SEGMENT_TRANSLATION")
        self.assertEqual(result["translated_segments"], [{
            "segment_id": "seg_0001",
            "translated_text_en": translated_text,
        }])
        self.assertEqual(result["items"], [])
        payload = captured["payloads"][0]
        self.assertEqual(payload["max_tokens"], 3072)
        self.assertEqual(
            payload["response_format"]["json_schema"]["name"],
            "compact_segment_translation",
        )
        prompt = payload["messages"][0]["content"]
        self.assertIn("Translate only the requested target segments", prompt)
        self.assertIn("each distinct medical expression", prompt)

    def test_keeps_complete_source_unchanged_while_translation_is_search_only(self):
        text = "환자는 코프가 증가했습니다. 오늘 날씨는 맑습니다."
        translated_text = "The patient's cough increased. The weather is clear today."
        server, thread, captured = self._serve_compact_translations({
            "t0001": translated_text,
        })
        segments = [{"id": "seg_0001", "start": 0.0, "end": 2.0, "text": text}]
        original = json.loads(json.dumps(segments, ensure_ascii=False))
        try:
            result = LlamaServerMedicalQueryExpander(
                f"http://127.0.0.1:{server.server_port}"
            ).expand(segments, covered_spans=[{
                "segment_id": "seg_0001",
                "text": "코프",
                "start_char": text.index("코프"),
                "end_char": text.index("코프") + 2,
            }])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(segments, original)
        supplied = json.loads(captured["payloads"][0]["messages"][1]["content"])
        self.assertEqual(
            supplied["context_segments"],
            [{"segment_id": "t0001", "text": text}],
        )
        self.assertEqual(result["items"], [])
        self.assertEqual(
            result["translated_segments"][0]["translated_text_en"],
            translated_text,
        )

    def test_invalid_model_json_falls_back_without_raising(self):
        server, thread, _ = self._serve({"unexpected": []})
        try:
            expander = LlamaServerMedicalQueryExpander(
                f"http://127.0.0.1:{server.server_port}"
            )
            result = expander.expand(
                [{"id": "seg_0001", "start": 0, "end": 1, "text": "코프"}]
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["error_code"], "InvalidModelResponse")

    def test_output_length_failure_is_reported_with_a_specific_error_code(self):
        server, thread, _ = self._serve(
            {"translations": {"t0001": "unfinished"}},
            finish_reason="length",
        )
        try:
            result = LlamaServerMedicalQueryExpander(
                f"http://127.0.0.1:{server.server_port}"
            ).expand(
                [{"id": "seg_0001", "start": 0, "end": 1, "text": "코프"}]
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error_code"], "OutputLengthExceeded")

class ExpandedRetrievalTests(unittest.TestCase):
    def test_workflow_reports_partial_translation_without_discarding_successes(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                return {
                    "status": "available",
                    "fallback_used": True,
                    "partial": True,
                    "error_code": "PartialTranslationFailure",
                    "failed_segment_ids": ["seg_0002"],
                    "translated_segments": [{
                        "segment_id": "seg_0001",
                        "translated_text_en": "Cough increased.",
                    }],
                    "items": [],
                }

        class Retriever:
            def retrieve(self, *, raw_text, context):
                return []

        class Extractor:
            def extract(self, payload):
                return {
                    "clinical_record": {},
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                }

        result = run_clinical_workflow(
            {"segments": [{
                "id": "seg_0001",
                "start": 0,
                "end": 1,
                "text": "코프가 증가했습니다.",
            }, {
                "id": "seg_0002",
                "start": 1,
                "end": 2,
                "text": "새로운 표현입니다.",
            }]},
            retriever=Retriever(),
            query_expander=Expander(),
            clinical_extractor=Extractor(),
        )

        self.assertEqual(result["processing_status"], "partial")
        self.assertTrue(result["query_expansion"]["fallback_used"])
        self.assertEqual(
            result["query_expansion"]["translated_segments"][0]["segment_id"],
            "seg_0001",
        )
        self.assertEqual(result["errors"], [{
            "stage": "query_expansion",
            "code": "PartialTranslationFailure",
            "detail": "Translation unavailable for segments: seg_0002",
        }])

    def test_compact_translation_phrases_are_searched_as_review_only_candidates(self):
        calls = []

        class Retriever:
            def retrieve(self, *, raw_text, context):
                calls.append(raw_text)
                if raw_text.casefold() != "eye pain":
                    return []
                return [{
                    "collection": "emergency_terms",
                    "entity_id": "symptom:eye-pain",
                    "canonical_ko": "안구통",
                    "canonical_en": "eye pain",
                    "entity_type": "symptom",
                    "source_text": "eye pain",
                    "start_char": 0,
                    "end_char": 8,
                    "match_type": "official_exact",
                    "review_status": "official",
                    "retrieval_score": 0.99,
                }]

        raw_text = "오른쪽 아이페인이 갑자기 생겼습니다."
        result = run_api3(
            {"segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 1.0,
                "text": raw_text,
            }]},
            retriever=Retriever(),
            query_expansion={
                "status": "available",
                "fallback_used": False,
                "method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                "translated_segments": [{
                    "segment_id": "seg_0001",
                    "translated_text_en": (
                        "Sudden right eye pain and blurred vision."
                    ),
                }],
                "items": [],
            },
            max_candidates_per_span=5,
        )

        self.assertIn("eye pain", [value.casefold() for value in calls])
        annotation = result["segments"][0]["annotations"][0]
        self.assertEqual(annotation["source_span"], {
            "text": raw_text,
            "start_char": 0,
            "end_char": len(raw_text),
        })
        self.assertTrue(annotation["needs_review"])
        candidate = annotation["candidates"][0]
        self.assertEqual(candidate["canonical_en"], "eye pain")
        self.assertEqual(candidate["match_type"], "gemma_translation_search")
        self.assertEqual(candidate["search_term_en"], "eye pain")

    def test_only_translation_queries_with_dictionary_hits_are_exposed_to_ui(self):
        class Retriever:
            def retrieve(self, *, raw_text, context):
                identities = {
                    "eye pain": ("symptom:eye-pain", "안구통", "eye pain"),
                    "blurred vision": (
                        "symptom:blurred-vision",
                        "시야 흐림",
                        "blurred vision",
                    ),
                }
                identity = identities.get(raw_text.casefold())
                if identity is None:
                    return []
                return [{
                    "collection": "emergency_terms",
                    "entity_id": identity[0],
                    "canonical_ko": identity[1],
                    "canonical_en": identity[2],
                    "source_text": identity[2],
                    "start_char": 0,
                    "end_char": len(identity[2]),
                    "match_type": "official_exact",
                    "review_status": "official",
                    "retrieval_score": 0.99,
                }]

        raw_text = "오른쪽 아이페인이 갑자기 생겼습니다."
        result = run_api3(
            {"segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 1.0,
                "text": raw_text,
            }]},
            retriever=Retriever(),
            query_expansion={
                "status": "available",
                "fallback_used": False,
                "translated_segments": [{
                    "segment_id": "seg_0001",
                    "translated_text_en": "Sudden right eye pain and blurred vision.",
                }],
                "items": [],
            },
            max_candidates_per_span=5,
        )

        self.assertEqual(result["query_expansion"]["items"], [{
            "segment_id": "seg_0001",
            "source_span": {
                "text": raw_text,
                "start_char": 0,
                "end_char": len(raw_text),
            },
            "search_terms_en": ["eye pain", "blurred vision"],
            "term_type": None,
            "expansion_method": "GEMMA_FULL_SEGMENT_TRANSLATION",
        }])

    def test_english_query_hits_are_mapped_back_to_raw_and_never_auto_applied(self):
        calls = []

        class Retriever:
            def retrieve(self, *, raw_text, context):
                calls.append(raw_text)
                if raw_text != "cough":
                    return []
                return [
                    {
                        "collection": "emergency_terms",
                        "entity_id": "symptom:cough",
                        "canonical_ko": "기침",
                        "canonical_en": "cough",
                        "entity_type": "symptom",
                        "source_text": "cough",
                        "start_char": 0,
                        "end_char": 5,
                        "match_type": "official_exact",
                        "review_status": "official",
                        "retrieval_score": 0.99,
                    }
                ]

        raw_text = "코프가 심해졌어요."
        expansion = {
            "status": "available",
            "fallback_used": False,
            "items": [
                {
                    "segment_id": "seg_0001",
                    "source_span": {
                        "text": "코프",
                        "start_char": 0,
                        "end_char": 2,
                    },
                    "search_terms_en": ["cough"],
                    "term_type": "symptom_or_sign",
                    "expansion_method": "GEMMA_CONTEXTUAL",
                }
            ],
        }
        result = run_api3(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": raw_text,
                    }
                ]
            },
            retriever=Retriever(),
            query_expansion=expansion,
            max_candidates_per_span=5,
        )

        segment = result["segments"][0]
        self.assertEqual(calls, [raw_text, "cough"])
        self.assertEqual(segment["raw_text"], raw_text)
        self.assertEqual(segment["corrected_text"], raw_text)
        self.assertEqual(segment["corrections"], [])
        annotation = segment["annotations"][0]
        self.assertEqual(annotation["source_span"]["text"], "코프")
        self.assertTrue(annotation["needs_review"])
        candidate = annotation["candidates"][0]
        self.assertEqual(candidate["canonical_en"], "cough")
        self.assertEqual(candidate["match_type"], "gemma_query_expansion")
        self.assertEqual(candidate["search_term_en"], "cough")
        self.assertEqual(candidate["review_status"], "needs_review")

    def test_dictionary_miss_preserves_the_detected_span_for_direct_clinician_review(self):
        class Retriever:
            def retrieve(self, *, raw_text, context):
                return []

        class Extractor:
            def extract(self, payload):
                return {
                    "clinical_record": {},
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                }

        class Expander:
            def expand(self, segments, *, covered_spans):
                return {
                    "status": "available",
                    "fallback_used": False,
                    "items": [{
                        "segment_id": "seg_0001",
                        "source_span": {
                            "text": "리네일러",
                            "start_char": 0,
                            "end_char": 4,
                        },
                        "search_terms_en": ["unknown inhaler device"],
                        "term_type": "device",
                        "expansion_method": "GEMMA_CONTEXTUAL",
                    }],
                }

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0,
                    "end": 1,
                    "text": "리네일러 사용 중입니다.",
                }]
            },
            retriever=Retriever(),
            query_expander=Expander(),
            clinical_extractor=Extractor(),
        )

        annotation = result["api3"]["segments"][0]["annotations"][0]
        self.assertEqual(annotation["type"], "unresolved_medical_term")
        self.assertEqual(annotation["source_span"]["text"], "리네일러")
        self.assertEqual(annotation["candidates"], [])
        self.assertTrue(annotation["needs_review"])
        self.assertEqual(annotation["search_terms_en"], ["unknown inhaler device"])
        review = result["draft"]["review_items"][0]
        self.assertEqual(review["source"], "리네일러")
        self.assertEqual(review["candidates"], [])
        self.assertEqual(review["search_terms_en"], ["unknown inhaler device"])

    def test_workflow_runs_expansion_before_retrieval_and_exposes_audit_contract(self):
        observed = {"retrieval": []}

        class Expander:
            def expand(self, segments, *, covered_spans):
                observed["expanded_segments"] = json.loads(
                    json.dumps(segments, ensure_ascii=False)
                )
                observed["covered_spans"] = covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "items": [
                        {
                            "segment_id": "seg_0001",
                            "source_span": {
                                "text": "코프",
                                "start_char": 0,
                                "end_char": 2,
                            },
                            "search_terms_en": ["cough"],
                            "term_type": "symptom_or_sign",
                            "expansion_method": "GEMMA_CONTEXTUAL",
                        }
                    ],
                }

        class Retriever:
            def retrieve(self, *, raw_text, context):
                observed["retrieval"].append(raw_text)
                if raw_text != "cough":
                    return []
                return [
                    {
                        "collection": "emergency_terms",
                        "entity_id": "symptom:cough",
                        "canonical_ko": "기침",
                        "canonical_en": "cough",
                        "entity_type": "symptom",
                        "source_text": "cough",
                        "start_char": 0,
                        "end_char": 5,
                        "match_type": "official_exact",
                        "review_status": "official",
                        "retrieval_score": 0.97,
                    }
                ]

        class Extractor:
            def extract(self, payload):
                observed["extractor_payload"] = payload
                return {
                    "clinical_record": {},
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                }

        payload = {
            "segments": [
                {
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "코프가 심해졌어요.",
                },
                {
                    "id": "seg_0002",
                    "start": 1.0,
                    "end": 2.0,
                    "text": "COPD가 있습니다.",
                },
            ]
        }
        result = run_clinical_workflow(
            payload,
            retriever=Retriever(),
            query_expander=Expander(),
            clinical_extractor=Extractor(),
        )

        self.assertEqual(observed["expanded_segments"], payload["segments"])
        self.assertEqual(
            observed["retrieval"],
            ["코프가 심해졌어요.", "cough", "COPD가 있습니다."],
        )
        self.assertEqual(result["query_expansion"]["status"], "available")
        self.assertNotIn("query_expansion", result["api3"])
        self.assertEqual(
            json.dumps(result, ensure_ascii=False).count('"query_expansion":'),
            1,
        )
        self.assertEqual(
            result["audit"]["schema_version"],
            "clinical-workflow-audit-v1",
        )
        self.assertEqual(
            result["audit"]["references"],
            {
                "query_expansion_path": "$.query_expansion",
                "segments_path": "$.api3.segments",
                "clinical_record_path": "$.api2.clinical_record",
                "candidate_decisions_path": "$.candidate_decisions",
                "errors_path": "$.errors",
            },
        )
        self.assertEqual(
            result["audit"]["versions"]["workflow_schema"],
            "clinical-workflow-v1",
        )
        self.assertEqual(
            result["audit"]["versions"]["api3_schema"],
            "clinical-stt-correction-v1",
        )
        self.assertNotIn("metadata", result["api3"])
        self.assertNotIn("metadata", result["api2"])
        self.assertEqual(
            set(result["audit"]["timestamps"]),
            {"api3_created_at", "clinical_record_created_at"},
        )
        self.assertNotIn(
            "translated_segments",
            json.dumps(result["audit"], ensure_ascii=False),
        )
        annotation = observed["extractor_payload"]["segments"][0]["annotations"][0]
        self.assertTrue(annotation["needs_review"])
        self.assertEqual(annotation["candidates"][0]["match_type"], "gemma_query_expansion")
        feedback_candidate = result["draft"]["review_items"][0]["candidate_details"][0]
        self.assertEqual(feedback_candidate["display_value"], "cough")
        self.assertEqual(feedback_candidate["entity_id"], "symptom:cough")
        self.assertEqual(feedback_candidate["source_entity_type"], "symptom")

    def test_workflow_translates_before_dictionary_retrieval(self):
        events = []
        text = "코프와 새증상"

        class Retriever:
            def retrieve(self, *, raw_text, context):
                events.append(("retrieve", raw_text))
                if raw_text == text:
                    return [{
                        "collection": "emergency_terms",
                        "entity_id": "symptom:cough",
                        "canonical_ko": "기침",
                        "canonical_en": "cough",
                        "entity_type": "symptom",
                        "source_text": "코프",
                        "start_char": 0,
                        "end_char": 2,
                        "match_type": "official_exact",
                        "review_status": "official",
                        "retrieval_score": 1.0,
                    }]
                if raw_text == "novel symptom":
                    return [{
                        "collection": "emergency_terms",
                        "entity_id": "symptom:novel",
                        "canonical_ko": "새 증상",
                        "canonical_en": "novel symptom",
                        "entity_type": "symptom",
                        "source_text": "novel symptom",
                        "start_char": 0,
                        "end_char": 13,
                        "match_type": "official_exact",
                        "review_status": "official",
                        "retrieval_score": 0.9,
                    }]
                return []

        class Expander:
            def expand(self, segments, *, covered_spans):
                events.append(("expand", None))
                self.covered_spans = covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "items": [{
                        "segment_id": "seg_0001",
                        "source_span": {
                            "text": "새증상",
                            "start_char": 4,
                            "end_char": 7,
                        },
                        "search_terms_en": ["novel symptom"],
                        "term_type": "symptom_or_sign",
                        "expansion_method": "GEMMA_CONTEXTUAL",
                    }],
                }

        class Extractor:
            def extract(self, payload):
                return {
                    "clinical_record": {},
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                }

        expander = Expander()
        result = run_clinical_workflow(
            {"segments": [{
                "id": "seg_0001",
                "start": 0,
                "end": 1,
                "text": text,
            }]},
            retriever=Retriever(),
            query_expander=expander,
            clinical_extractor=Extractor(),
        )

        self.assertEqual(
            events,
            [
                ("expand", None),
                ("retrieve", text),
                ("retrieve", "novel symptom"),
            ],
        )
        self.assertEqual(expander.covered_spans, [])
        sources = [
            annotation["source_span"]["text"]
            for annotation in result["api3"]["segments"][0]["annotations"]
        ]
        self.assertEqual(sources, ["코프", "새증상"])

    def test_workflow_falls_back_to_existing_retrieval_when_expander_fails(self):
        calls = []

        class Expander:
            def expand(self, segments, *, covered_spans):
                raise TimeoutError("local model timed out")

        class Retriever:
            def retrieve(self, *, raw_text, context):
                calls.append(raw_text)
                return []

        class Extractor:
            def extract(self, payload):
                return {
                    "clinical_record": {},
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0,
                        "end": 1,
                        "text": "새로운 발음의 의학용어",
                    }
                ]
            },
            retriever=Retriever(),
            query_expander=Expander(),
            clinical_extractor=Extractor(),
        )

        self.assertEqual(calls, ["새로운 발음의 의학용어"])
        self.assertEqual(result["processing_status"], "partial")
        self.assertEqual(
            result["errors"],
            [
                {
                    "stage": "query_expansion",
                    "code": "TimeoutError",
                    "detail": (
                        "Query expansion unavailable; "
                        "dictionary-only fallback used"
                    ),
                }
            ],
        )
        self.assertEqual(result["query_expansion"]["status"], "unavailable")
        self.assertTrue(result["query_expansion"]["fallback_used"])
        self.assertEqual(result["query_expansion"]["error_code"], "TimeoutError")

    def test_resolver_failure_preserves_official_raw_exact_fallback(self):
        calls = []

        class Expander:
            def expand(self, segments, *, covered_spans):
                return {
                    "status": "available",
                    "fallback_used": False,
                    "items": [],
                }

        class Resolver:
            mode = "umls_primary"

            def resolve(self, document):
                raise RuntimeError("UMLS worker crashed")

        class OfficialRawExactFallback:
            def retrieve(self, *, raw_text, context):
                calls.append(raw_text)
                return [{
                    "collection": "emergency_terms",
                    "entity_id": "emergency:cough",
                    "canonical_ko": "기침",
                    "canonical_en": "cough",
                    "source_text": "기침",
                    "start_char": 0,
                    "end_char": 2,
                    "match_type": "official_exact",
                    "review_status": "official",
                    "retrieval_score": 1.0,
                }]

        class Extractor:
            def extract(self, payload):
                return {
                    "clinical_record": {},
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0,
                    "end": 1,
                    "text": "기침",
                }]
            },
            retriever=OfficialRawExactFallback(),
            query_expander=Expander(),
            medical_query_resolver=Resolver(),
            clinical_extractor=Extractor(),
            include_query_resolution_summary=True,
        )

        self.assertEqual(calls, ["기침"])
        self.assertEqual(
            result["api3"]["segments"][0]["annotations"][0]["candidates"][0][
                "match_type"
            ],
            "official_exact",
        )
        self.assertEqual(result["query_resolution"]["status"], "partial")

if __name__ == "__main__":
    unittest.main()
