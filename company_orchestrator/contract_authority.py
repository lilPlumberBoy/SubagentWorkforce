from __future__ import annotations

from typing import Any


def normalize_contract_reference(value: str | None) -> str:
    return str(value or "").strip().replace("\\", "/").lower()


def is_frontend_consumption_contract_path(path: str | None) -> bool:
    normalized = normalize_contract_reference(path)
    return normalized.endswith("/frontend-api-consumption-contract.md") or normalized.endswith(
        "frontend-api-consumption-contract.md"
    )


def contract_kind_for_reference(
    *,
    path: str | None = None,
    asset_id: str | None = None,
    output_id: str | None = None,
) -> str | None:
    normalized_path = normalize_contract_reference(path)
    normalized_asset_id = normalize_contract_reference(asset_id)
    normalized_output_id = normalize_contract_reference(output_id)

    if (
        is_frontend_consumption_contract_path(path)
        or "consumption-contract" in normalized_path
        or normalized_output_id.endswith("consumption-contract")
    ):
        return "consumer"
    if (
        normalized_asset_id.endswith(":integration-contract")
        or "integration-contract" in normalized_path
        or normalized_output_id.endswith("integration-contract")
    ):
        return "integration"
    if (
        normalized_asset_id.endswith(":api-contract")
        or "api-interface-contract" in normalized_path
        or "api-contract" in normalized_path
        or "openapi" in normalized_path
        or normalized_output_id.endswith("api-contract")
        or normalized_output_id.endswith("api_contract")
    ):
        return "api"
    return None


def contract_kind_for_descriptor(descriptor: dict[str, Any]) -> str | None:
    return contract_kind_for_reference(
        path=str(descriptor.get("path") or ""),
        asset_id=str(descriptor.get("asset_id") or ""),
        output_id=str(descriptor.get("output_id") or ""),
    )


def authoritative_capability_for_contract_kind(kind: str | None) -> str | None:
    if kind == "api":
        return "backend"
    if kind == "integration":
        return "middleware"
    return None


def capability_may_author_contract(capability: str, kind: str | None) -> bool:
    if kind == "consumer":
        return capability == "frontend"
    owner = authoritative_capability_for_contract_kind(kind)
    if owner is None:
        return True
    return capability == owner
