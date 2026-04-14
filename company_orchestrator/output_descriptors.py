from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

OUTPUT_DESCRIPTOR_PATTERN = re.compile(r"^(asset\.[A-Za-z0-9._:-]+)\s+in\s+([A-Za-z0-9_./-]+)$")
REPO_PATH_PATTERN = re.compile(r"^(?:\./)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.*?@:-]+)+/?$")
NULL_SENTINELS = {"none", "null"}


def sanitize_output_descriptors(values: list[Any] | None) -> list[Any]:
    sanitized: list[Any] = []
    for value in values or []:
        if isinstance(value, dict):
            updated = dict(value)
            kind = str(updated.get("kind", "") or "").strip()
            if kind == "assertion":
                updated["path"] = None
                updated["asset_id"] = None
            sanitized.append(updated)
            continue
        sanitized.append(value)
    return sanitized


def normalize_output_descriptors(values: list[Any] | None, *, allow_legacy_strings: bool = True) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values or []:
        descriptor = normalize_output_descriptor(value, allow_legacy_strings=allow_legacy_strings)
        if descriptor is None:
            continue
        output_id = descriptor_output_id(descriptor)
        if output_id in seen:
            continue
        seen.add(output_id)
        normalized.append(descriptor)
    return normalized


def normalize_output_descriptor(value: Any, *, allow_legacy_strings: bool = True) -> dict[str, Any] | None:
    if isinstance(value, dict):
        kind = normalize_required_text_field(value.get("kind"), field_name="kind", output_id="output-descriptor")
        output_id = normalize_required_text_field(value.get("output_id"), field_name="output_id", output_id="output-descriptor")
        if not kind or not output_id:
            raise ValueError("output descriptor objects must include kind and output_id")
        if kind == "artifact":
            path = normalize_required_text_field(value.get("path"), field_name="path", output_id=output_id)
            return {
                "kind": "artifact",
                "output_id": output_id,
                "path": path,
                "asset_id": None,
                "description": None,
                "evidence": None,
            }
        if kind == "asset":
            asset_id = normalize_required_text_field(value.get("asset_id"), field_name="asset_id", output_id=output_id)
            path = normalize_required_text_field(value.get("path"), field_name="path", output_id=output_id)
            return {
                "kind": "asset",
                "output_id": output_id,
                "asset_id": asset_id,
                "path": path,
                "description": None,
                "evidence": None,
            }
        if kind == "assertion":
            if "path" not in value:
                raise ValueError(f"assertion output {output_id} must include path set to null")
            if value.get("path") is not None:
                invalid_path = normalize_optional_text_field(value.get("path"), field_name="path", output_id=output_id)
                if invalid_path is not None:
                    raise ValueError(f"assertion output {output_id} must set path to null")
            description = normalize_required_text_field(
                value.get("description"),
                field_name="description",
                output_id=output_id,
            )
            evidence = value.get("evidence", {})
            if not isinstance(evidence, dict):
                evidence = {}
            validation_ids = normalize_string_list(
                evidence.get("validation_ids", []),
                field_name="evidence.validation_ids",
                output_id=output_id,
            )
            artifact_paths = normalize_string_list(
                evidence.get("artifact_paths", []),
                field_name="evidence.artifact_paths",
                output_id=output_id,
            )
            return {
                "kind": "assertion",
                "output_id": output_id,
                "path": None,
                "asset_id": None,
                "description": description,
                "evidence": {
                    "validation_ids": validation_ids,
                    "artifact_paths": artifact_paths,
                },
            }
        raise ValueError(f"unsupported output descriptor kind {kind!r}")
    if not allow_legacy_strings:
        if value is None:
            return None
        raise ValueError(f"legacy string outputs are not allowed: {value!r}")
    if not isinstance(value, str):
        return None
    return legacy_output_descriptor(value)


def legacy_output_descriptor(value: str) -> dict[str, Any] | None:
    normalized = value.strip()
    if not normalized:
        return None
    asset_id, output_path = split_legacy_asset_descriptor(normalized)
    if asset_id is not None and output_path is not None:
        return {
            "kind": "asset",
            "output_id": f"asset:{asset_id}",
            "asset_id": asset_id,
            "path": output_path,
            "description": None,
            "evidence": None,
        }
    if looks_like_repo_path(normalized) and not normalized.endswith(".v1"):
        return {
            "kind": "artifact",
            "output_id": f"artifact:{normalized}",
            "path": normalized,
            "asset_id": None,
            "description": None,
            "evidence": None,
        }
    return {
        "kind": "assertion",
        "output_id": f"legacy:{slugify_text(normalized)}:{short_hash(normalized)}",
        "path": None,
        "asset_id": None,
        "description": normalized,
        "evidence": {
            "validation_ids": [],
            "artifact_paths": [],
        },
    }


def descriptor_output_id(descriptor: dict[str, Any]) -> str:
    return str(descriptor.get("output_id", "")).strip()


def descriptor_kind(descriptor: dict[str, Any]) -> str:
    return str(descriptor.get("kind", "")).strip()


def descriptor_asset_id(descriptor: dict[str, Any]) -> str | None:
    asset_id = descriptor.get("asset_id")
    if isinstance(asset_id, str) and asset_id.strip():
        return asset_id.strip()
    return None


def descriptor_path(descriptor: dict[str, Any]) -> str | None:
    try:
        path = normalize_optional_text_field(
            descriptor.get("path"),
            field_name="path",
            output_id=descriptor_output_id(descriptor) or "output-descriptor",
        )
    except ValueError:
        return None
    if path is not None:
        return path
    evidence = descriptor.get("evidence")
    if not isinstance(evidence, dict):
        return None
    artifact_paths = normalize_string_list(
        evidence.get("artifact_paths", []),
        field_name="evidence.artifact_paths",
        output_id=descriptor_output_id(descriptor) or "output-descriptor",
    )
    if len(artifact_paths) == 1:
        return artifact_paths[0]
    return None


def descriptor_summary(descriptor: dict[str, Any]) -> str:
    kind = descriptor_kind(descriptor)
    output_id = descriptor_output_id(descriptor)
    if kind == "artifact":
        path = descriptor_path(descriptor) or ""
        return f"{output_id} -> {path}"
    if kind == "asset":
        asset_id = descriptor_asset_id(descriptor) or ""
        path = descriptor_path(descriptor) or ""
        return f"{output_id} ({asset_id}) -> {path}"
    if kind == "assertion":
        description = str(descriptor.get("description", "")).strip()
        return f"{output_id} :: {description}"
    return output_id


def output_descriptor_paths(values: list[Any] | None, *, allow_legacy_strings: bool = True) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for descriptor in normalize_output_descriptors(values, allow_legacy_strings=allow_legacy_strings):
        path = descriptor_path(descriptor)
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def output_descriptor_ids(values: list[Any] | None, *, allow_legacy_strings: bool = True) -> list[str]:
    return [
        descriptor_output_id(descriptor)
        for descriptor in normalize_output_descriptors(values, allow_legacy_strings=allow_legacy_strings)
    ]


def output_descriptor_map(values: list[Any] | None, *, allow_legacy_strings: bool = True) -> dict[str, dict[str, Any]]:
    return {
        descriptor_output_id(descriptor): descriptor
        for descriptor in normalize_output_descriptors(values, allow_legacy_strings=allow_legacy_strings)
    }


def split_legacy_asset_descriptor(value: str) -> tuple[str | None, str | None]:
    matched = OUTPUT_DESCRIPTOR_PATTERN.match(value.strip())
    if matched is None:
        return None, None
    return matched.group(1), matched.group(2)


def looks_like_repo_path(value: str) -> bool:
    normalized = value.strip()
    if not normalized or " " in normalized:
        return False
    return REPO_PATH_PATTERN.match(normalized) is not None


def slugify_text(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:40] or "output"


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def repo_relative_path_exists(search_roots: list[Path], path_value: str) -> bool:
    path = Path(path_value)
    if path.is_absolute():
        return path.exists()
    normalized = str(path_value or "").strip()
    if any(token in normalized for token in "*?["):
        for root in search_roots:
            try:
                if any(root.glob(normalized)):
                    return True
            except Exception:
                continue
        return False
    return any((root / path).exists() for root in search_roots)


def normalize_required_text_field(value: Any, *, field_name: str, output_id: str) -> str:
    normalized = normalize_optional_text_field(value, field_name=field_name, output_id=output_id)
    if normalized is None:
        raise ValueError(f"{field_name} on output {output_id} must be a non-empty string")
    return normalized


def normalize_optional_text_field(value: Any, *, field_name: str, output_id: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} on output {output_id} must be a string or null")
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.lower() in NULL_SENTINELS:
        raise ValueError(f"{field_name} on output {output_id} must not use stringified null sentinels")
    return normalized


def normalize_string_list(values: Any, *, field_name: str, output_id: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError(f"{field_name} on output {output_id} must be an array")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = normalize_optional_text_field(item, field_name=field_name, output_id=output_id)
        if value is None or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized
