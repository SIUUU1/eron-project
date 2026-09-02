from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
from typing import Any
from urllib.request import Request, urlopen


class InvalidClinicalLlmOutput(ValueError):
    """The provider response did not satisfy the requested JSON contract."""


@dataclass(frozen=True)
class _ChatResult:
    content: str
    done_reason: str | None
    eval_count: int | None


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
    if "const" in schema and value != schema["const"]:
        raise InvalidClinicalLlmOutput(f"{path} does not match the required value")
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        matching_variants = 0
        for variant in one_of:
            if not isinstance(variant, dict):
                continue
            try:
                _validate_schema(value, variant, path)
            except InvalidClinicalLlmOutput:
                continue
            matching_variants += 1
        if matching_variants != 1:
            raise InvalidClinicalLlmOutput(
                f"{path} must match exactly one allowed schema variant"
            )
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for variant in all_of:
            if isinstance(variant, dict):
                _validate_schema(value, variant, path)
    conditional = schema.get("if")
    if isinstance(conditional, dict):
        try:
            _validate_schema(value, conditional, path)
        except InvalidClinicalLlmOutput:
            branch = schema.get("else")
        else:
            branch = schema.get("then")
        if isinstance(branch, dict):
            _validate_schema(value, branch, path)

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
        minimum = schema.get("minProperties")
        maximum = schema.get("maxProperties")
        if isinstance(minimum, int) and len(value) < minimum:
            raise InvalidClinicalLlmOutput(f"{path} has too few properties")
        if isinstance(maximum, int) and len(value) > maximum:
            raise InvalidClinicalLlmOutput(f"{path} has too many properties")
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        missing = [key for key in required if key not in value]
        if missing:
            raise InvalidClinicalLlmOutput(f"{path} is missing required fields")
        additional_properties = schema.get("additionalProperties")
        if additional_properties is False:
            unexpected = set(value) - set(properties)
            if unexpected:
                raise InvalidClinicalLlmOutput(f"{path} has unexpected fields")
        for key, item in value.items():
            property_schema = properties.get(key)
            if isinstance(property_schema, dict):
                _validate_schema(item, property_schema, f"{path}.{key}")
            elif isinstance(additional_properties, dict):
                _validate_schema(
                    item,
                    additional_properties,
                    f"{path}.{key}",
                )

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
        contains_schema = schema.get("contains")
        if isinstance(contains_schema, dict):
            contains_match = False
            for index, item in enumerate(value):
                try:
                    _validate_schema(
                        item,
                        contains_schema,
                        f"{path}[{index}]",
                    )
                except InvalidClinicalLlmOutput:
                    continue
                contains_match = True
                break
            if not contains_match:
                raise InvalidClinicalLlmOutput(
                    f"{path} does not contain the required item"
                )
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


def _invalid_issue(content: str, schema: dict[str, Any]) -> str:
    objects = _json_objects(content)
    if not objects:
        return "no complete JSON object"
    try:
        _validate_schema(objects[0], schema)
    except InvalidClinicalLlmOutput as exc:
        return str(exc)
    return "no root object satisfied the contract"


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
    """Ollama Cloud adapter with one repair and one bounded regeneration."""

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

    def _chat(self, messages: list[dict[str, str]]) -> _ChatResult:
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
        done_reason = result.get("done_reason")
        eval_count = result.get("eval_count")
        return _ChatResult(
            content=content,
            done_reason=done_reason if isinstance(done_reason, str) else None,
            eval_count=eval_count if isinstance(eval_count, int) else None,
        )

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
        first = self._chat(messages)
        valid = self._valid_object(first.content, schema)
        if valid is not None:
            return valid

        repair_prompt = (
            "Repair the prior assistant output so it is exactly one JSON object "
            "that satisfies the supplied JSON Schema. Preserve the prior factual "
            "content. Do not add facts, explanations, or Markdown. JSON Schema: "
            + schema_text
        )
        repaired = self._chat(
            [
                {"role": "system", "content": repair_prompt},
                {"role": "user", "content": first.content},
            ]
        )
        valid = self._valid_object(repaired.content, schema)
        if valid is not None:
            return valid

        regeneration = self._chat(messages)
        valid = self._valid_object(regeneration.content, schema)
        if valid is not None:
            return valid
        raise InvalidClinicalLlmOutput(
            f"Ollama Cloud returned no valid {output_label} JSON after one repair "
            "and one regeneration; "
            f"initial_done_reason={first.done_reason or 'unknown'}; "
            f"initial_eval_count={first.eval_count}; "
            f"initial_chars={len(first.content)}; "
            f"initial_issue={_invalid_issue(first.content, schema)}; "
            f"repair_done_reason={repaired.done_reason or 'unknown'}; "
            f"repair_eval_count={repaired.eval_count}; "
            f"repair_chars={len(repaired.content)}; "
            f"repair_issue={_invalid_issue(repaired.content, schema)}; "
            f"regeneration_done_reason={regeneration.done_reason or 'unknown'}; "
            f"regeneration_eval_count={regeneration.eval_count}; "
            f"regeneration_chars={len(regeneration.content)}; "
            f"regeneration_issue={_invalid_issue(regeneration.content, schema)}"
        )

