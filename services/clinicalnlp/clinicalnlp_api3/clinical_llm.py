from __future__ import annotations

from abc import ABC, abstractmethod
import json
from typing import Any
from urllib.request import Request, urlopen


class InvalidClinicalLlmOutput(ValueError):
    """The provider response did not satisfy the requested JSON contract."""


def _json_objects(content: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    cleaned = content.replace("```json", "").replace("```JSON", "").replace(
        "```", ""
    )
    objects: list[dict[str, Any]] = []
    for position, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected]
    expected_types = [item for item in expected_types if isinstance(item, str)]
    if expected_types and not any(_matches_type(value, item) for item in expected_types):
        raise InvalidClinicalLlmOutput(
            f"{path} has the wrong type; expected {' or '.join(expected_types)}"
        )
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidClinicalLlmOutput(f"{path} is outside the allowed values")

    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        missing = [key for key in required if key not in value]
        if missing:
            raise InvalidClinicalLlmOutput(f"{path} is missing required fields")
        if schema.get("additionalProperties") is False:
            unexpected = set(value) - set(properties)
            if unexpected:
                raise InvalidClinicalLlmOutput(f"{path} has unexpected fields")
        for key, item in value.items():
            property_schema = properties.get(key)
            if isinstance(property_schema, dict):
                _validate_schema(item, property_schema, f"{path}.{key}")

    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise InvalidClinicalLlmOutput(f"{path} has too few items")
        if isinstance(maximum, int) and len(value) > maximum:
            raise InvalidClinicalLlmOutput(f"{path} has too many items")
        if schema.get("uniqueItems") is True:
            serialized = [
                json.dumps(item, ensure_ascii=False, sort_keys=True)
                for item in value
            ]
            if len(serialized) != len(set(serialized)):
                raise InvalidClinicalLlmOutput(f"{path} has duplicate items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema(item, item_schema, f"{path}[{index}]")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise InvalidClinicalLlmOutput(f"{path} is too short")
        if isinstance(maximum, int) and len(value) > maximum:
            raise InvalidClinicalLlmOutput(f"{path} is too long")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise InvalidClinicalLlmOutput(f"{path} is below the minimum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise InvalidClinicalLlmOutput(f"{path} is above the maximum")


def _response_schema(response_format: dict[str, Any]) -> dict[str, Any]:
    json_schema = response_format.get("json_schema")
    schema = json_schema.get("schema") if isinstance(json_schema, dict) else None
    if not isinstance(schema, dict):
        raise ValueError("response_format must contain a JSON schema")
    return schema


class ClinicalLlmClient(ABC):
    @abstractmethod
    def generate_json(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
        response_format: dict[str, Any],
        output_label: str,
    ) -> dict[str, Any]:
        raise NotImplementedError


class OllamaCloudClinicalLlmClient(ClinicalLlmClient):
    """Native Ollama Cloud chat adapter with one bounded JSON repair."""

    def __init__(
        self,
        base_url: str,
        *,
        model_name: str,
        api_key: str,
        max_output_tokens: int = 3072,
        timeout: float = 600,
    ):
        if not api_key.strip():
            raise ValueError("OLLAMA_API_KEY is required for Ollama Cloud")
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout

    def _chat(self, messages: list[dict[str, str]]) -> str:
        body = json.dumps(
            {
                "model": self.model_name,
                "messages": messages,
                "stream": False,
                "think": False,
                "options": {
                    "temperature": 0,
                    "num_predict": self.max_output_tokens,
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/api/chat",
            data=body,
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        message = result.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise InvalidClinicalLlmOutput("Ollama Cloud returned no text content")
        return content

    @staticmethod
    def _valid_object(
        content: str, schema: dict[str, Any]
    ) -> dict[str, Any] | None:
        for candidate in _json_objects(content):
            try:
                _validate_schema(candidate, schema)
            except InvalidClinicalLlmOutput:
                continue
            return candidate
        return None

    def generate_json(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
        response_format: dict[str, Any],
        output_label: str,
    ) -> dict[str, Any]:
        schema = _response_schema(response_format)
        schema_text = json.dumps(
            schema, ensure_ascii=False, separators=(",", ":")
        )
        contracted_system_prompt = (
            system_prompt.rstrip()
            + "\n\nReturn exactly one JSON object and no Markdown or explanation. "
            "The object must satisfy this JSON Schema: "
            + schema_text
        )
        messages = [
            {"role": "system", "content": contracted_system_prompt},
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False),
            },
        ]
        first_content = self._chat(messages)
        valid = self._valid_object(first_content, schema)
        if valid is not None:
            return valid

        repair_prompt = (
            "Repair the prior assistant output so it is exactly one JSON object "
            "that satisfies the supplied JSON Schema. Preserve the prior factual "
            "content. Do not add facts, explanations, or Markdown. JSON Schema: "
            + schema_text
        )
        repaired_content = self._chat(
            [
                {"role": "system", "content": repair_prompt},
                {"role": "user", "content": first_content},
            ]
        )
        valid = self._valid_object(repaired_content, schema)
        if valid is not None:
            return valid
        raise InvalidClinicalLlmOutput(
            f"Ollama Cloud returned no valid {output_label} JSON after one repair"
        )

