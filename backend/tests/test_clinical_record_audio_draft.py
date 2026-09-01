import json
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx2

from app.api.clinical_records import (
    get_clinicalnlp_client,
    get_whisper_client,
    router,
)
from app.services.clinicalnlp import ClinicalNlpClient
from app.services.whisper import WhisperClient


def valid_workflow() -> dict:
    field_ids = (
        "chief_complaint",
        "pain_assessment",
        "history_of_present_illness",
        "past_history",
        "medications",
        "allergy",
        "social_history",
        "review_of_systems",
        "physical_examination",
        "treatment_plan",
        "impression",
        "outcome",
    )
    fields = {
        field_id: {
            "field_id": field_id,
            "value": "",
            "ai_original_value": "",
            "suggestion_status": "UNCHANGED",
            "applied_candidates": [],
            "information_status": "NOT_ASSESSED",
            "evidence": [],
        }
        for field_id in field_ids
    }
    return {
        "schema_version": "clinical-workflow-v2",
        "processing_status": "completed",
        "record_status": "DRAFT",
        "workflow_phase": "DRAFT_GENERATION",
        "validation": {
            "status": "PASS",
            "issues": [],
            "rule_applicability": {
                "G16": "NOT_APPLICABLE",
                "G17": "NOT_APPLICABLE",
                "G18": "NOT_APPLICABLE",
            },
        },
        "completed_at": None,
        "api3": {"segments": []},
        "api2": {"clinical_record": {}},
        "query_expansion": {},
        "candidate_decisions": [],
        "audit": {
            "schema_version": "clinical-workflow-audit-v1",
            "references": {
                "query_expansion_path": "$.query_expansion",
                "segments_path": "$.api3.segments",
                "clinical_record_path": "$.api2.clinical_record",
                "candidate_decisions_path": "$.candidate_decisions",
                "errors_path": "$.errors",
            },
            "versions": {
                "workflow_schema": "clinical-workflow-v2",
                "api3_schema": None,
                "clinical_record_schema": None,
                "model": "gemma4:31b",
                "clinical_prompt": None,
                "candidate_prompt": None,
                "alias_db": None,
            },
            "timestamps": {
                "api3_created_at": None,
                "clinical_record_created_at": None,
            },
        },
        "draft": {"fields": fields, "review_items": []},
        "errors": [],
    }


class ClinicalRecordAudioDraftApiTests(unittest.TestCase):
    def test_audio_transcription_returns_whisper_payload_without_running_clinicalnlp(self):
        whisper_payload = {
            "api_version": "v1",
            "status": "completed",
            "language": "ko",
            "segments": [
                {
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "합성 흉통 문장",
                    "speaker": "SPEAKER_00",
                }
            ],
        }

        def stt_upstream(request: httpx2.Request) -> httpx2.Response:
            if request.method == "POST":
                return httpx2.Response(
                    202,
                    json={
                        "id": "a" * 32,
                        "status": "queued",
                        "links": {
                            "self": f"/v1/transcriptions/{'a' * 32}",
                            "result": f"/v1/transcriptions/{'a' * 32}/result",
                        },
                    },
                )
            if request.url.path.endswith("/result"):
                return httpx2.Response(200, json=whisper_payload)
            return httpx2.Response(200, json={"status": "completed"})

        app = FastAPI()
        app.include_router(router)

        async def override_whisper_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(stt_upstream)
            ) as http_client:
                yield WhisperClient(
                    base_url="http://whisper:8780",
                    timeout_seconds=30,
                    poll_interval_seconds=0,
                    http_client=http_client,
                )

        app.dependency_overrides[get_whisper_client] = override_whisper_client

        with TestClient(app) as client:
            response = client.post(
                "/api/clinical-records/transcribe",
                files={"audio": ("synthetic.wav", b"synthetic-audio", "audio/wav")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), whisper_payload)

    def test_audio_is_transcribed_then_forwarded_to_clinicalnlp(self):
        whisper_payload = {
            "api_version": "v1",
            "status": "completed",
            "language": "ko",
            "segments": [
                {
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "합성 흉통 문장",
                }
            ],
        }
        workflow = valid_workflow()
        stt_requests = []

        def stt_upstream(request: httpx2.Request) -> httpx2.Response:
            stt_requests.append((request.method, request.url.path))
            if request.method == "POST":
                self.assertNotIn("X-API-Key", request.headers)
                return httpx2.Response(
                    202,
                    json={
                        "id": "a" * 32,
                        "status": "queued",
                        "links": {
                            "self": f"/v1/transcriptions/{'a' * 32}",
                            "result": f"/v1/transcriptions/{'a' * 32}/result",
                        },
                    },
                )
            if request.url.path.endswith("/result"):
                return httpx2.Response(200, json=whisper_payload)
            return httpx2.Response(200, json={"status": "completed"})

        def clinical_upstream(request: httpx2.Request) -> httpx2.Response:
            self.assertEqual(
                str(request.url),
                "http://clinicalnlp:8765/v2/clinical-workflows",
            )
            self.assertEqual(json.loads(request.content), whisper_payload)
            return httpx2.Response(200, json=workflow)

        app = FastAPI()
        app.include_router(router)

        async def override_whisper_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(stt_upstream)
            ) as http_client:
                yield WhisperClient(
                    base_url="http://whisper:8780",
                    timeout_seconds=30,
                    poll_interval_seconds=0,
                    http_client=http_client,
                )

        async def override_clinical_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(clinical_upstream)
            ) as http_client:
                yield ClinicalNlpClient(
                    base_url="http://clinicalnlp:8765",
                    timeout_seconds=180,
                    http_client=http_client,
                )

        app.dependency_overrides[get_whisper_client] = override_whisper_client
        app.dependency_overrides[get_clinicalnlp_client] = override_clinical_client

        with TestClient(app) as client:
            response = client.post(
                "/api/clinical-records/draft/audio",
                files={"audio": ("synthetic.wav", b"synthetic-audio", "audio/wav")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), workflow)
        self.assertEqual(
            stt_requests,
            [
                ("POST", "/v1/transcriptions"),
                ("GET", f"/v1/transcriptions/{'a' * 32}"),
                ("GET", f"/v1/transcriptions/{'a' * 32}/result"),
            ],
        )

    def test_rejected_audio_returns_400_without_clinicalnlp_call(self):
        def stt_upstream(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                415,
                json={"error": {"code": "UNSUPPORTED_MEDIA_TYPE"}},
            )

        def clinical_upstream(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError("ClinicalNLP must not run for rejected audio")

        app = FastAPI()
        app.include_router(router)

        async def override_whisper_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(stt_upstream)
            ) as http_client:
                yield WhisperClient(
                    base_url="http://whisper:8780",
                    timeout_seconds=30,
                    poll_interval_seconds=0,
                    http_client=http_client,
                )

        async def override_clinical_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(clinical_upstream)
            ) as http_client:
                yield ClinicalNlpClient(
                    base_url="http://clinicalnlp:8765",
                    timeout_seconds=180,
                    http_client=http_client,
                )

        app.dependency_overrides[get_whisper_client] = override_whisper_client
        app.dependency_overrides[get_clinicalnlp_client] = override_clinical_client

        with TestClient(app) as client:
            response = client.post(
                "/api/clinical-records/draft/audio",
                files={"audio": ("not-audio.txt", b"invalid", "text/plain")},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_audio"})


if __name__ == "__main__":
    unittest.main()
