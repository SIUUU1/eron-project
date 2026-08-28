import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from clinicalnlp_api3.clinical_llm import (
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
                content = responses[len(captured) - 1]
                body = json.dumps(
                    {
                        "model": "gemma4:31b",
                        "message": {"role": "assistant", "content": content},
                        "done": True,
                        "done_reason": "stop",
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

    def test_rejects_output_that_remains_invalid_after_one_repair(self):
        server, thread, captured = self._serve(["not json", '{"ok": "yes"}'])
        try:
            client = OllamaCloudClinicalLlmClient(
                f"http://127.0.0.1:{server.server_port}",
                model_name="gemma4:31b",
                api_key="test-secret",
            )
            with self.assertRaisesRegex(
                InvalidClinicalLlmOutput,
                "no valid test output JSON after one repair",
            ):
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

        self.assertEqual(len(captured), 2)


if __name__ == "__main__":
    unittest.main()

