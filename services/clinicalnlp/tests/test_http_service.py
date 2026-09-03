import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from clinicalnlp_api3.http_service import create_http_server


class ClinicalNlpHttpServiceTests(unittest.TestCase):
    def test_health_reports_ready_without_exposing_configuration(self):
        class Runtime:
            pass

        server = create_http_server(
            "127.0.0.1",
            0,
            runtime=Runtime(),
            request_timeout_seconds=1,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(
                f"http://127.0.0.1:{server.server_port}/health",
                timeout=3,
            ) as response:
                status = response.status
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(status, 200)
        self.assertEqual(
            result,
            {
                "schema_version": "clinicalnlp-health-v1",
                "status": "ready",
            },
        )

    def test_readiness_blocks_draft_requests_until_dependencies_are_ready(self):
        state = {"ready": False}

        class Runtime:
            call_count = 0

            def generate_draft(self, payload):
                self.call_count += 1
                return {
                    "schema_version": "clinical-workflow-v2",
                    "processing_status": "completed",
                    "record_status": "DRAFT",
                }

        runtime = Runtime()
        server = create_http_server(
            "127.0.0.1",
            0,
            runtime=runtime,
            request_timeout_seconds=1,
            readiness_probe=lambda: state["ready"],
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        body = json.dumps(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "가슴이 아파요",
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            with self.assertRaises(HTTPError) as raised:
                urlopen(
                    f"http://127.0.0.1:{server.server_port}/ready",
                    timeout=3,
                )
            self.assertEqual(raised.exception.code, 503)
            self.assertEqual(
                json.loads(raised.exception.read().decode("utf-8")),
                {
                    "schema_version": "clinicalnlp-readiness-v1",
                    "status": "not_ready",
                    "reason": "startup",
                },
            )

            request = Request(
                f"http://127.0.0.1:{server.server_port}/v2/clinical-workflows",
                data=body,
                headers={"content-type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 503)
            self.assertEqual(
                json.loads(raised.exception.read().decode("utf-8")),
                {
                    "error": "clinicalnlp_unavailable",
                    "reason": "startup",
                },
            )
            self.assertEqual(runtime.call_count, 0)

            state["ready"] = True
            with urlopen(
                f"http://127.0.0.1:{server.server_port}/ready",
                timeout=3,
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(
                    json.loads(response.read().decode("utf-8")),
                    {
                        "schema_version": "clinicalnlp-readiness-v1",
                        "status": "ready",
                    },
                )
            with urlopen(request, timeout=3) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(runtime.call_count, 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_valid_whisper_payload_returns_the_v2_draft(self):
        expected = {
            "schema_version": "clinical-workflow-v2",
            "processing_status": "completed",
            "record_status": "DRAFT",
        }

        class Runtime:
            def generate_draft(self, payload):
                return expected

        server = create_http_server(
            "127.0.0.1",
            0,
            runtime=Runtime(),
            request_timeout_seconds=1,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0.0,
                            "end": 1.0,
                            "text": "가슴이 아파요",
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = Request(
                f"http://127.0.0.1:{server.server_port}/v2/clinical-workflows",
                data=body,
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=3) as response:
                status = response.status
                result = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(status, 200)
        self.assertEqual(result, expected)

    def test_invalid_whisper_payload_is_rejected_before_runtime_execution(self):
        class Runtime:
            def generate_draft(self, payload):
                raise AssertionError("invalid input must not reach the runtime")

        server = create_http_server(
            "127.0.0.1",
            0,
            runtime=Runtime(),
            request_timeout_seconds=1,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/v2/clinical-workflows",
                data=json.dumps({"segments": [{"text": "id가 없음"}]}).encode(
                    "utf-8"
                ),
                headers={"content-type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            status = raised.exception.code
            result = json.loads(raised.exception.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(status, 400)
        self.assertEqual(result, {"error": "invalid_whisper_payload"})

    def test_unavailable_runtime_returns_a_sanitized_service_error(self):
        server = create_http_server(
            "127.0.0.1",
            0,
            runtime=None,
            request_timeout_seconds=1,
            unavailable_reason="configuration",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "숨이 차요",
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = Request(
                f"http://127.0.0.1:{server.server_port}/v2/clinical-workflows",
                data=body,
                headers={"content-type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            status = raised.exception.code
            result = json.loads(raised.exception.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(status, 503)
        self.assertEqual(
            result,
            {
                "error": "clinicalnlp_unavailable",
                "reason": "configuration",
            },
        )

    def test_runtime_deadline_returns_gateway_timeout(self):
        class Runtime:
            def generate_draft(self, payload):
                time.sleep(0.1)
                return {
                    "schema_version": "clinical-workflow-v2",
                    "processing_status": "completed",
                }

        server = create_http_server(
            "127.0.0.1",
            0,
            runtime=Runtime(),
            request_timeout_seconds=0.01,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "숨이 차요",
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = Request(
                f"http://127.0.0.1:{server.server_port}/v2/clinical-workflows",
                data=body,
                headers={"content-type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            status = raised.exception.code
            result = json.loads(raised.exception.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(status, 504)
        self.assertEqual(result, {"error": "clinicalnlp_timeout"})

    def test_invalid_runtime_contract_returns_bad_gateway(self):
        class Runtime:
            def generate_draft(self, payload):
                return {"schema_version": "unexpected-contract"}

        server = create_http_server(
            "127.0.0.1",
            0,
            runtime=Runtime(),
            request_timeout_seconds=1,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "숨이 차요",
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = Request(
                f"http://127.0.0.1:{server.server_port}/v2/clinical-workflows",
                data=body,
                headers={"content-type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            status = raised.exception.code
            result = json.loads(raised.exception.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(status, 502)
        self.assertEqual(result, {"error": "invalid_clinicalnlp_response"})

    def test_runtime_failure_returns_sanitized_service_unavailable(self):
        class Runtime:
            def generate_draft(self, payload):
                raise OSError("upstream failure with sensitive details")

        server = create_http_server(
            "127.0.0.1",
            0,
            runtime=Runtime(),
            request_timeout_seconds=1,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(
                {
                    "segments": [
                        {
                            "id": "seg_0001",
                            "start": 0,
                            "end": 1,
                            "text": "숨이 차요",
                        }
                    ]
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = Request(
                f"http://127.0.0.1:{server.server_port}/v2/clinical-workflows",
                data=body,
                headers={"content-type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            status = raised.exception.code
            result = json.loads(raised.exception.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(status, 503)
        self.assertEqual(result, {"error": "clinicalnlp_unavailable"})


if __name__ == "__main__":
    unittest.main()
