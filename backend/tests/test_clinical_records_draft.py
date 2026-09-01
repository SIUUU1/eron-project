import json
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx2

from app.api.clinical_records import get_clinicalnlp_client, router
from app.services.clinicalnlp import ClinicalNlpClient


FIELD_IDS = (
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


def valid_workflow() -> dict:
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
        for field_id in FIELD_IDS
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


def app_for_upstream(upstream) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    async def override_client():
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(upstream)
        ) as http_client:
            yield ClinicalNlpClient(
                base_url="http://clinicalnlp:8765",
                timeout_seconds=180,
                http_client=http_client,
            )

    app.dependency_overrides[get_clinicalnlp_client] = override_client
    return app


class ClinicalRecordDraftApiTests(unittest.TestCase):
    def test_valid_whisper_payload_returns_the_clinical_workflow(self):
        whisper_payload = {
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

        def upstream(request: httpx2.Request) -> httpx2.Response:
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                str(request.url),
                "http://clinicalnlp:8765/v2/clinical-workflows",
            )
            self.assertEqual(json.loads(request.content), whisper_payload)
            return httpx2.Response(200, json=workflow)

        app = FastAPI()
        app.include_router(router)

        async def override_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(upstream)
            ) as http_client:
                yield ClinicalNlpClient(
                    base_url="http://clinicalnlp:8765",
                    timeout_seconds=180,
                    http_client=http_client,
                )

        app.dependency_overrides[get_clinicalnlp_client] = override_client

        with TestClient(app) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json=whisper_payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), workflow)

    def test_invalid_whisper_payload_returns_400_before_upstream_call(self):
        def upstream(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError("invalid input must not reach ClinicalNLP")

        app = FastAPI()
        app.include_router(router)

        async def override_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(upstream)
            ) as http_client:
                yield ClinicalNlpClient(
                    base_url="http://clinicalnlp:8765",
                    timeout_seconds=180,
                    http_client=http_client,
                )

        app.dependency_overrides[get_clinicalnlp_client] = override_client

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json={"segments": [{"start": 0, "end": 1, "text": "ID 없음"}]},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_whisper_payload"})

    def test_invalid_clinicalnlp_workflow_returns_502(self):
        def upstream(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                json={
                    "schema_version": "clinical-workflow-v2",
                    "processing_status": "completed",
                    "record_status": "DRAFT",
                },
            )

        app = FastAPI()
        app.include_router(router)

        async def override_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(upstream)
            ) as http_client:
                yield ClinicalNlpClient(
                    base_url="http://clinicalnlp:8765",
                    timeout_seconds=180,
                    http_client=http_client,
                )

        app.dependency_overrides[get_clinicalnlp_client] = override_client

        with TestClient(app) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json={
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "합성 문장",
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "invalid_clinicalnlp_response"})

    def test_clinicalnlp_connection_failure_returns_503(self):
        def upstream(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("connection refused", request=request)

        app = FastAPI()
        app.include_router(router)

        async def override_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(upstream)
            ) as http_client:
                yield ClinicalNlpClient(
                    base_url="http://clinicalnlp:8765",
                    timeout_seconds=180,
                    http_client=http_client,
                )

        app.dependency_overrides[get_clinicalnlp_client] = override_client

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json={
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "합성 문장",
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "clinicalnlp_unavailable"})

    def test_clinicalnlp_timeout_returns_504(self):
        def upstream(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("deadline exceeded", request=request)

        app = FastAPI()
        app.include_router(router)

        async def override_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(upstream)
            ) as http_client:
                yield ClinicalNlpClient(
                    base_url="http://clinicalnlp:8765",
                    timeout_seconds=180,
                    http_client=http_client,
                )

        app.dependency_overrides[get_clinicalnlp_client] = override_client

        with TestClient(app) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json={
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "합성 문장",
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json(), {"error": "clinicalnlp_timeout"})

    def test_missing_clinicalnlp_configuration_returns_503(self):
        app = FastAPI()
        app.include_router(router)

        with TestClient(app) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json={
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "합성 문장",
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "clinicalnlp_unavailable"})

    def test_clinicalnlp_unavailable_response_returns_503(self):
        def upstream(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(503, json={"error": "clinicalnlp_unavailable"})

        app = FastAPI()
        app.include_router(router)

        async def override_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(upstream)
            ) as http_client:
                yield ClinicalNlpClient(
                    base_url="http://clinicalnlp:8765",
                    timeout_seconds=180,
                    http_client=http_client,
                )

        app.dependency_overrides[get_clinicalnlp_client] = override_client

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json={
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "합성 문장",
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "clinicalnlp_unavailable"})

    def test_clinicalnlp_invalid_input_response_returns_400(self):
        def upstream(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(400, json={"error": "invalid_whisper_payload"})

        app = FastAPI()
        app.include_router(router)

        async def override_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(upstream)
            ) as http_client:
                yield ClinicalNlpClient(
                    base_url="http://clinicalnlp:8765",
                    timeout_seconds=180,
                    http_client=http_client,
                )

        app.dependency_overrides[get_clinicalnlp_client] = override_client

        with TestClient(app) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json={
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "합성 문장",
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_whisper_payload"})

    def test_clinicalnlp_timeout_response_returns_504(self):
        def upstream(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(504, json={"error": "clinicalnlp_timeout"})

        app = FastAPI()
        app.include_router(router)

        async def override_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(upstream)
            ) as http_client:
                yield ClinicalNlpClient(
                    base_url="http://clinicalnlp:8765",
                    timeout_seconds=180,
                    http_client=http_client,
                )

        app.dependency_overrides[get_clinicalnlp_client] = override_client

        with TestClient(app) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json={
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "합성 문장",
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json(), {"error": "clinicalnlp_timeout"})

    def test_completed_record_from_clinicalnlp_returns_502(self):
        workflow = valid_workflow()
        workflow["record_status"] = "COMPLETED"
        workflow["workflow_phase"] = "FINALIZATION"
        workflow["completed_at"] = "2026-08-28T10:00:00+09:00"

        def upstream(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=workflow)

        app = FastAPI()
        app.include_router(router)

        async def override_client():
            async with httpx2.AsyncClient(
                transport=httpx2.MockTransport(upstream)
            ) as http_client:
                yield ClinicalNlpClient(
                    base_url="http://clinicalnlp:8765",
                    timeout_seconds=180,
                    http_client=http_client,
                )

        app.dependency_overrides[get_clinicalnlp_client] = override_client

        with TestClient(app) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json={
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "합성 문장",
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "invalid_clinicalnlp_response"})

    def test_partial_clinical_workflow_is_preserved(self):
        workflow = valid_workflow()
        workflow["processing_status"] = "partial"
        workflow["errors"] = [{"code": "query_expansion_unavailable"}]

        def upstream(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=workflow)

        with TestClient(app_for_upstream(upstream)) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json={
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "합성 문장",
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), workflow)

    def test_non_json_clinicalnlp_response_returns_502(self):
        def upstream(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                content=b"not-json",
                headers={"content-type": "text/plain"},
            )

        with TestClient(app_for_upstream(upstream)) as client:
            response = client.post(
                "/api/clinical-records/draft",
                json={
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "합성 문장",
                        }
                    ]
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {"error": "invalid_clinicalnlp_response"})

    def test_malformed_request_json_returns_400(self):
        def upstream(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError("invalid JSON must not reach ClinicalNLP")

        with TestClient(app_for_upstream(upstream)) as client:
            response = client.post(
                "/api/clinical-records/draft",
                content=b"{",
                headers={"content-type": "application/json"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "invalid_whisper_payload"})


if __name__ == "__main__":
    unittest.main()
