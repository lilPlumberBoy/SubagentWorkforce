from __future__ import annotations

from pathlib import Path
from typing import Any

from .constants import PHASES
from .filesystem import read_json, write_json
from .schemas import validate_document


AUTO_APPROVED_FALSE_FLAGS = (
    "goal_changed",
    "scope_changed",
    "boundary_changed",
    "architecture_changed",
    "team_changed",
)
AUTO_APPROVED_TRUE_FLAGS = ("interface_changed", "implementation_changed")


def _trimmed_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def classify_change_request_approval(impact: dict[str, Any]) -> dict[str, str]:
    if any(bool(impact.get(flag)) for flag in AUTO_APPROVED_FALSE_FLAGS):
        return {"mode": "human", "status": "pending_human_review"}
    if any(bool(impact.get(flag)) for flag in AUTO_APPROVED_TRUE_FLAGS):
        return {"mode": "auto", "status": "approved"}
    raise ValueError(
        "Change request must alter shared interface or implementation when broader scope/boundary flags are false."
    )


def normalize_change_request_payloads(payloads: Any) -> list[dict[str, Any]]:
    if payloads is None:
        return []
    if not isinstance(payloads, list):
        raise ValueError("change_requests must be an array when present.")
    normalized: list[dict[str, Any]] = []
    seen_fingerprints: set[tuple[Any, ...]] = set()
    for index, raw in enumerate(payloads):
        if not isinstance(raw, dict):
            raise ValueError(f"change_requests[{index}] must be an object.")
        change_category = str(raw.get("change_category", "")).strip()
        summary = str(raw.get("summary", "")).strip()
        blocking_reason = str(raw.get("blocking_reason", "")).strip()
        why_local_invalid = str(raw.get("why_local_resolution_is_invalid", "")).strip()
        required_reentry_phase = str(raw.get("required_reentry_phase", "")).strip()
        if not change_category:
            raise ValueError(f"change_requests[{index}] must include a change_category.")
        if not summary:
            raise ValueError(f"change_requests[{index}] must include a summary.")
        if not blocking_reason:
            raise ValueError(f"change_requests[{index}] must include a blocking_reason.")
        if not why_local_invalid:
            raise ValueError(f"change_requests[{index}] must include why_local_resolution_is_invalid.")
        if not required_reentry_phase:
            raise ValueError(f"change_requests[{index}] must include a required_reentry_phase.")
        if raw.get("blocking") is not True:
            raise ValueError(f"change_requests[{index}] must be blocking=true.")
        if raw.get("goal_critical") is not True:
            raise ValueError(f"change_requests[{index}] must be goal_critical=true.")
        affected_output_ids = _trimmed_string_list(raw.get("affected_output_ids"))
        affected_handoff_ids = _trimmed_string_list(raw.get("affected_handoff_ids"))
        if not affected_output_ids and not affected_handoff_ids:
            raise ValueError(
                f"change_requests[{index}] must reference affected_output_ids or affected_handoff_ids."
            )
        impact = raw.get("impact")
        if not isinstance(impact, dict):
            raise ValueError(f"change_requests[{index}] must include an impact object.")
        approval = classify_change_request_approval(impact)
        normalized_request = {
            "change_category": change_category,
            "summary": summary,
            "blocking_reason": blocking_reason,
            "why_local_resolution_is_invalid": why_local_invalid,
            "blocking": True,
            "goal_critical": True,
            "affected_output_ids": affected_output_ids,
            "affected_handoff_ids": affected_handoff_ids,
            "impacted_objective_ids": _trimmed_string_list(raw.get("impacted_objective_ids")),
            "impacted_task_ids": _trimmed_string_list(raw.get("impacted_task_ids")),
            "required_reentry_phase": required_reentry_phase,
            "impact": {
                "goal_changed": bool(impact.get("goal_changed")),
                "scope_changed": bool(impact.get("scope_changed")),
                "boundary_changed": bool(impact.get("boundary_changed")),
                "interface_changed": bool(impact.get("interface_changed")),
                "architecture_changed": bool(impact.get("architecture_changed")),
                "team_changed": bool(impact.get("team_changed")),
                "implementation_changed": bool(impact.get("implementation_changed")),
            },
            "approval": approval,
        }
        fingerprint = (
            normalized_request["change_category"],
            normalized_request["blocking_reason"].lower(),
            tuple(normalized_request["affected_output_ids"]),
            tuple(normalized_request["affected_handoff_ids"]),
        )
        if fingerprint in seen_fingerprints:
            raise ValueError(
                f"change_requests[{index}] duplicates another request with the same root blocker and affected outputs."
            )
        seen_fingerprints.add(fingerprint)
        normalized.append(normalized_request)
    return normalized


def next_change_request_id(run_dir: Path, task_id: str) -> str:
    requests_dir = run_dir / "change-requests"
    requests_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{task_id}-chg-"
    max_index = 0
    for path in requests_dir.glob(f"{prefix}*.json"):
        suffix = path.stem.replace(prefix, "", 1)
        if suffix.isdigit():
            max_index = max(max_index, int(suffix))
    return f"{prefix}{max_index + 1:03d}"


def persist_change_requests(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    normalized_requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    persisted: list[dict[str, Any]] = []
    for request in normalized_requests:
        change_id = next_change_request_id(run_dir, task["task_id"])
        payload = {
            "schema": "change-request.v2",
            "run_id": run_id,
            "change_id": change_id,
            "source_task_id": task["task_id"],
            "source_objective_id": task["objective_id"],
            "phase": task["phase"],
            **request,
            "replacement_plan_revision": None,
        }
        validate_document(payload, "change-request.v2", project_root)
        write_json(run_dir / "change-requests" / f"{change_id}.json", payload)
        persisted.append(payload)
    return persisted


def active_approved_change_requests(
    project_root: Path,
    run_id: str,
    change_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    requests_dir = run_dir / "change-requests"
    if not requests_dir.exists():
        return []
    selected_ids = set(change_ids or [])
    requests: list[dict[str, Any]] = []
    for path in sorted(requests_dir.glob("*.json")):
        payload = read_json(path)
        if selected_ids and payload["change_id"] not in selected_ids:
            continue
        if payload.get("approval", {}).get("status") != "approved":
            continue
        if payload.get("replacement_plan_revision") is not None:
            continue
        requests.append(payload)
    return requests


def earliest_required_reentry_phase(change_requests: list[dict[str, Any]]) -> str | None:
    if not change_requests:
        return None
    phase_order = {phase: index for index, phase in enumerate(PHASES)}
    return min(
        (str(request["required_reentry_phase"]) for request in change_requests),
        key=lambda phase: phase_order[phase],
    )


def mark_change_requests_replanned(
    project_root: Path,
    run_id: str,
    change_ids: list[str],
    *,
    replacement_plan_revision: str,
) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    requests_dir = run_dir / "change-requests"
    if not requests_dir.exists():
        return []
    selected_ids = set(change_ids)
    updated_requests: list[dict[str, Any]] = []
    for path in sorted(requests_dir.glob("*.json")):
        payload = read_json(path)
        if payload["change_id"] not in selected_ids:
            continue
        payload["replacement_plan_revision"] = replacement_plan_revision
        validate_document(payload, "change-request.v2", project_root)
        write_json(path, payload)
        updated_requests.append(payload)
    return updated_requests
