"""Validate real response bodies against contracts/openapi.yaml (test-plan.md «Contract» row).

The schemas are read from the contract file itself and every `$ref` is resolved there, so a test
never carries its own copy of a schema. Only the JSON Schema keywords the file uses are supported;
any other keyword raises, so a schema change can never be skipped silently.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

CONTRACT = Path(__file__).resolve().parents[2] / "docs/features/exchange-adapter/contracts/openapi.yaml"
_ANNOTATIONS = {"description", "example", "examples", "summary", "title"}
_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


class ContractViolation(AssertionError):
    pass


@lru_cache(maxsize=1)
def _document() -> dict:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def _resolve(node: dict) -> dict:
    while "$ref" in node:
        target: object = _document()
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target  # type: ignore[assignment]
    return node


def _operation(operation_id: str) -> dict:
    for path_item in _document()["paths"].values():
        for operation in path_item.values():
            if operation.get("operationId") == operation_id:
                return operation
    raise ContractViolation(f"no operation {operation_id!r} in {CONTRACT.name}")


def _response(operation_id: str, status: int) -> dict:
    responses = _operation(operation_id)["responses"]
    if str(status) not in responses:
        raise ContractViolation(f"{operation_id} does not declare status {status}: {sorted(responses)}")
    return _resolve(responses[str(status)])


def _is_type(value, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def validate(value, schema: dict, where: str = "$") -> None:
    schema = _resolve(schema)
    unknown = set(schema) - _ANNOTATIONS - {
        "type", "required", "properties", "additionalProperties", "items", "enum", "const",
        "pattern", "minLength", "maxLength", "minimum", "oneOf", "anyOf", "allOf", "format",
    }
    if unknown:
        raise ContractViolation(f"{where}: the checker does not support {sorted(unknown)}")

    for part in schema.get("allOf", []):
        validate(value, part, where)
    if "oneOf" in schema:
        matches = sum(_matches(value, part, where) for part in schema["oneOf"])
        if matches != 1:
            raise ContractViolation(f"{where}: {value!r} matches {matches} of the oneOf branches, not exactly 1")
    if "anyOf" in schema and not any(_matches(value, part, where) for part in schema["anyOf"]):
        raise ContractViolation(f"{where}: {value!r} matches none of the anyOf branches")
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, name) for name in names):
            raise ContractViolation(f"{where}: {value!r} is not of type {schema['type']}")
    if "const" in schema and value != schema["const"]:
        raise ContractViolation(f"{where}: {value!r} is not {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractViolation(f"{where}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ContractViolation(f"{where}: {value!r} does not match {schema['pattern']}")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            raise ContractViolation(f"{where}: {value!r} has a length outside the contract bounds")
    if isinstance(value, (int, float)) and "minimum" in schema and value < schema["minimum"]:
        raise ContractViolation(f"{where}: {value!r} is below {schema['minimum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ContractViolation(f"{where}: required property {key!r} is missing")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], f"{where}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ContractViolation(f"{where}: property {key!r} is not allowed")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{where}[{index}]")


def _matches(value, schema: dict, where: str) -> bool:
    try:
        validate(value, schema, where)
    except ContractViolation:
        return False
    return True


def assert_body_matches(operation_id: str, status: int, body) -> None:
    """The body is one the contract allows for this operation and status."""
    response = _response(operation_id, status)
    validate(body, response["content"]["application/json"]["schema"])


def assert_response_matches(operation_id: str, response) -> None:
    """A real HTTP response: declared status, schema-valid JSON body and declared const headers."""
    declared = _response(operation_id, response.status_code)
    for name, header in declared.get("headers", {}).items():
        const = header.get("schema", {}).get("const")
        if const is not None and response.headers.get(name) != const:
            raise ContractViolation(f"{operation_id} {response.status_code}: header {name} is not {const!r}")
    validate(response.json(), declared["content"]["application/json"]["schema"])
