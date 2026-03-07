from __future__ import annotations

from pathlib import Path
from typing import Any

from .constants import SCHEMA_NAMES, schema_root
from .filesystem import read_json


class SchemaValidationError(ValueError):
    pass


def load_schema(name: str, start: str | Path | None = None) -> dict[str, Any]:
    filename = SCHEMA_NAMES[name]
    return read_json(schema_root(start) / filename)


def validate_document(document: Any, schema_name: str, start: str | Path | None = None) -> None:
    schema = load_schema(schema_name, start)
    _validate(schema_name, document, schema, "$")


def _validate(schema_name: str, value: Any, schema: dict[str, Any], pointer: str) -> None:
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        if value is None and "null" in schema_type:
            return
        non_null_types = [candidate for candidate in schema_type if candidate != "null"]
        for candidate in non_null_types:
            candidate_schema = dict(schema)
            candidate_schema["type"] = candidate
            try:
                _validate(schema_name, value, candidate_schema, pointer)
                return
            except SchemaValidationError:
                continue
        allowed_types = ", ".join(schema_type)
        raise SchemaValidationError(f"{schema_name}: {pointer} must match one of {allowed_types}")
    if schema_type == "object":
        if not isinstance(value, dict):
            raise SchemaValidationError(f"{schema_name}: {pointer} must be an object")
        for key in schema.get("required", []):
            if key not in value:
                raise SchemaValidationError(f"{schema_name}: missing required key {pointer}.{key}")
        allowed = schema.get("additionalProperties", True)
        props = schema.get("properties", {})
        if allowed is False:
            extra_keys = set(value) - set(props)
            if extra_keys:
                extras = ", ".join(sorted(extra_keys))
                raise SchemaValidationError(f"{schema_name}: unexpected keys at {pointer}: {extras}")
        for key, prop_schema in props.items():
            if key in value:
                _validate(schema_name, value[key], prop_schema, f"{pointer}.{key}")
        return
    if schema_type == "array":
        if not isinstance(value, list):
            raise SchemaValidationError(f"{schema_name}: {pointer} must be an array")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                _validate(schema_name, item, item_schema, f"{pointer}[{index}]")
        return
    if schema_type == "string":
        if not isinstance(value, str):
            raise SchemaValidationError(f"{schema_name}: {pointer} must be a string")
        allowed = schema.get("enum")
        if allowed and value not in allowed:
            values = ", ".join(allowed)
            raise SchemaValidationError(f"{schema_name}: {pointer} must be one of {values}")
        return
    if schema_type == "boolean":
        if not isinstance(value, bool):
            raise SchemaValidationError(f"{schema_name}: {pointer} must be a boolean")
        return
    if schema_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise SchemaValidationError(f"{schema_name}: {pointer} must be a number")
        return
    if schema_type is None:
        return
    raise SchemaValidationError(f"{schema_name}: unsupported schema type {schema_type} at {pointer}")
