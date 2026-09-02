import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from clinicalnlp_api3.clinical_llm import (
    ClinicalLlmLengthLimit,
    InvalidClinicalLlmOutput,
    OllamaCloudClinicalLlmClient,
)


_OK_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "ok_result",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    },
}


class OllamaCloudClinicalLlmClientTests(unittest.TestCase):
    def _serve(self, responses):
        captured = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_POST(self):
                length = int(self.headers["content-length"])
                captured.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("authorization"),
                        "payload": json.loads(self.rfile.read(length)),
                    }
                )
                response_item = responses[len(captured) - 1]
                content = (
                    response_item.get("content", "")
                    if isinstance(response_item, dict)
                    else response_item
                )
                done_reason = (
                    response_item.get("done_reason", "stop")
                    if isinstance(response_item, dict)
                    else "stop"
                )
                body = json.dumps(
                    {
                        "model": "gemma4:31b",
                        "message": {"role": "assistant", "content": content},
                        "done": True,
                        "done_reason": done_reason,
                        "prompt_eval_count": 12,
                        "eval_count": 5,
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
        return server, thread, captured

    def test_uses_native_cloud_chat_contract_and_extracts_fenced_json(self):
        server, thread, captured = self._serve(["Result:\n```json\n{\"ok\": true}\n```"])
        try:
            client = OllamaCloudClinicalLlmClient(
                f"http://127.0.0.1:{server.server_port}",
                model_name="gemma4:31b",
                api_key="test-secret",
                max_output_tokens=2048,
            )
            result = client.generate_json(
                system_prompt="Return JSON.",
                user_payload={"synthetic": True},
                response_format=_OK_RESPONSE_FORMAT,
                output_label="test output",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["path"], "/api/chat")
        self.assertEqual(captured[0]["authorization"], "Bearer test-secret")
        self.assertEqual(captured[0]["payload"]["model"], "gemma4:31b")
        self.assertFalse(captured[0]["payload"]["stream"])
        self.assertFalse(captured[0]["payload"]["think"])
        self.assertEqual(captured[0]["payload"]["options"]["temperature"], 0)
        self.assertEqual(captured[0]["payload"]["options"]["num_predict"], 2048)
        self.assertNotIn("format", captured[0]["payload"])
        initial_system_prompt = captured[0]["payload"]["messages"][0]["content"]
        self.assertIn("JSON Schema", initial_system_prompt)
        self.assertIn('"required":["ok"]', initial_system_prompt)

    def test_repairs_schema_invalid_output_at_most_once(self):
        server, thread, captured = self._serve(
            ['{"ok": true, "unexpected": "discard me"}', '{"ok": true}']
        )
        try:
            client = OllamaCloudClinicalLlmClient(
                f"http://127.0.0.1:{server.server_port}",
                model_name="gemma4:31b",
                api_key="test-secret",
            )
            result = client.generate_json(
                system_prompt="Return JSON.",
                user_payload={"synthetic": True},
                response_format=_OK_RESPONSE_FORMAT,
                output_label="test output",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(captured), 2)
        repair_messages = captured[1]["payload"]["messages"]
        self.assertIn("Repair", repair_messages[0]["content"])
        self.assertIn("unexpected", repair_messages[1]["content"])

    def test_length_result_skips_same_size_repair_and_regeneration(self):
        server, thread, captured = self._serve([
            {"content": '{"ok":', "done_reason": "length"}
        ])
        try:
            client = OllamaCloudClinicalLlmClient(
                f"http://127.0.0.1:{server.server_port}",
                model_name="gemma4:31b",
                api_key="test-secret",
            )
            with self.assertRaises(ClinicalLlmLengthLimit):
                client.generate_json(
                    system_prompt="Return JSON.",
                    user_payload={"synthetic": True},
                    response_format=_OK_RESPONSE_FORMAT,
                    output_label="test output",
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(len(captured), 1)
        self.assertEqual(client.last_diagnostics()["repair_count"], 0)
        self.assertEqual(client.last_diagnostics()["regeneration_count"], 0)

    def test_regenerates_once_from_original_context_when_repair_is_invalid(self):
        server, thread, captured = self._serve(
            ["not json", '{"ok": "yes"}', '{"ok": true}']
        )
        try:
            client = OllamaCloudClinicalLlmClient(
                f"http://127.0.0.1:{server.server_port}",
                model_name="gemma4:31b",
                api_key="test-secret",
            )
            result = client.generate_json(
                system_prompt="Return JSON.",
                user_payload={"synthetic": True},
                response_format=_OK_RESPONSE_FORMAT,
                output_label="test output",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(captured), 3)
        self.assertEqual(
            captured[2]["payload"]["messages"],
            captured[0]["payload"]["messages"],
        )

    def test_rejects_output_that_remains_invalid_after_bounded_recovery(self):
        server, thread, captured = self._serve(
            ["not json", '{"ok": "yes"}', '{"ok": "still yes"}']
        )
        try:
            client = OllamaCloudClinicalLlmClient(
                f"http://127.0.0.1:{server.server_port}",
                model_name="gemma4:31b",
                api_key="test-secret",
            )
            with self.assertRaises(InvalidClinicalLlmOutput) as raised:
                client.generate_json(
                    system_prompt="Return JSON.",
                    user_payload={"synthetic": True},
                    response_format=_OK_RESPONSE_FORMAT,
                    output_label="test output",
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(len(captured), 3)
        detail = str(raised.exception)
        self.assertIn("no valid test output JSON after one repair", detail)
        self.assertIn("initial_done_reason=stop", detail)
        self.assertIn("initial_issue=no complete JSON object", detail)
        self.assertIn("repair_done_reason=stop", detail)
        self.assertIn("repair_issue=$.ok has the wrong type", detail)
        self.assertIn("regeneration_done_reason=stop", detail)
        self.assertIn("regeneration_issue=$.ok has the wrong type", detail)

    def test_repairs_invalid_dynamic_one_of_candidate_reference(self):
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "candidate_fact",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["facts"],
                    "properties": {
                        "facts": {
                            "type": "object",
                            "maxProperties": 1,
                            "additionalProperties": {
                                "oneOf": [{
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["type", "candidate_ref"],
                                    "properties": {
                                        "type": {"const": "MATCHED_TERM"},
                                        "candidate_ref": {
                                            "type": "string",
                                            "enum": ["cr_allowed"],
                                        },
                                    },
                                }, {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["type", "text"],
                                    "properties": {
                                        "type": {"const": "NARRATIVE"},
                                        "text": {"type": "string"},
                                    },
                                }]
                            },
                        }
                    },
                },
            },
        }
        server, thread, captured = self._serve([
            '{"facts":{"f1":{"type":"MATCHED_TERM","candidate_ref":"cr_invented"}}}',
            '{"facts":{"f1":{"type":"MATCHED_TERM","candidate_ref":"cr_allowed"}}}',
        ])
        try:
            client = OllamaCloudClinicalLlmClient(
                f"http://127.0.0.1:{server.server_port}",
                model_name="gemma4:31b",
                api_key="test-secret",
            )
            result = client.generate_json(
                system_prompt="Return one candidate fact.",
                user_payload={"synthetic": True},
                response_format=response_format,
                output_label="candidate fact",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(
            result["facts"]["f1"]["candidate_ref"],
            "cr_allowed",
        )
        self.assertEqual(len(captured), 2)

    def test_repairs_array_missing_required_contains_item(self):
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "evidence_segments",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["candidate_ref", "segments"],
                    "properties": {
                        "candidate_ref": {
                            "type": "string",
                            "enum": ["cr_one", "cr_two"],
                        },
                        "segments": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["seg_0001", "seg_0002"],
                            },
                        }
                    },
                    "allOf": [{
                        "if": {
                            "required": ["candidate_ref"],
                            "properties": {
                                "candidate_ref": {"const": "cr_one"}
                            },
                        },
                        "then": {
                            "properties": {
                                "segments": {"contains": {"const": "seg_0001"}}
                            }
                        },
                    }],
                },
            },
        }
        server, thread, captured = self._serve(
            [
                '{"candidate_ref":"cr_one","segments":["seg_0002"]}',
                '{"candidate_ref":"cr_one","segments":["seg_0001","seg_0002"]}',
            ]
        )
        try:
            client = OllamaCloudClinicalLlmClient(
                f"http://127.0.0.1:{server.server_port}",
                model_name="gemma4:31b",
                api_key="test-secret",
            )
            result = client.generate_json(
                system_prompt="Return JSON.",
                user_payload={"synthetic": True},
                response_format=response_format,
                output_label="evidence segments",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result["segments"], ["seg_0001", "seg_0002"])
        self.assertEqual(len(captured), 2)


if __name__ == "__main__":
    unittest.main()

