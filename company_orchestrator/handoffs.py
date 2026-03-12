from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .filesystem import read_json, write_json
from .live import now_timestamp, record_event
from .schemas import validate_document


HANDOFF_PLANNED = "planned"
HANDOFF_WAITING_ON_SOURCE = "waiting_on_source"
HANDOFF_SATISFIED = "satisfied"
HANDOFF_BLOCKED = "blocked"
PENDING_HANDOFF_STATUSES = {HANDOFF_PLANNED, HANDOFF_WAITING_ON_SOURCE}


def normalize_handoff_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized.setdefault("to_task_ids", [])
    normalized.setdefault("satisfied_by_task_ids", [])
    normalized.setdefault("missing_deliverables", [])
    normalized.setdefault("status_reason", None)
    normalized.setdefault("last_checked_at", None)
    return normalized


def list_handoffs(run_dir: Path, *, phase: str | None = None) -> list[dict[str, Any]]:
    collaboration_dir = run_dir / "collaboration-plans"
    if not collaboration_dir.exists():
        return []
    handoffs: list[dict[str, Any]] = []
    for path in sorted(collaboration_dir.glob("*.json")):
        try:
            payload = normalize_handoff_payload(read_json(path))
        except json.JSONDecodeError:
            # A dashboard poll should not crash the whole run if a handoff file is
            # observed between create/replace operations. The next refresh will pick
            # up the completed file.
            continue
        if phase is None or payload["phase"] == phase:
            handoffs.append(payload)
    return handoffs


def refresh_handoffs_for_phase(
    project_root: Path,
    run_id: str,
    phase: str,
    tasks_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    refreshed: dict[str, dict[str, Any]] = {}
    for handoff in list_handoffs(run_dir, phase=phase):
        updated = evaluate_handoff(project_root, run_id, handoff, tasks_by_id)
        refreshed[updated["handoff_id"]] = updated
    return refreshed


def evaluate_handoff(
    project_root: Path,
    run_id: str,
    handoff: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    handoff = normalize_handoff_payload(handoff)
    previous_status = handoff["status"]
    previous_reason = handoff.get("status_reason")
    source_report_path = run_dir / "reports" / f"{handoff['from_task_id']}.json"
    source_execution_path = run_dir / "executions" / f"{handoff['from_task_id']}.json"

    status = handoff["status"]
    status_reason = handoff.get("status_reason")
    satisfied_by_task_ids: list[str] = []
    missing_deliverables: list[str] = []

    if not source_report_path.exists():
        status = HANDOFF_WAITING_ON_SOURCE
        status_reason = f"Waiting for source task {handoff['from_task_id']} to produce a completion report."
    else:
        report = read_json(source_report_path)
        report_status = report.get("status")
        if report_status != "ready_for_bundle_review":
            status = HANDOFF_BLOCKED if report_status in {"blocked", "needs_revision"} else HANDOFF_WAITING_ON_SOURCE
            if status == HANDOFF_BLOCKED:
                status_reason = f"Source task {handoff['from_task_id']} is not ready: {report_status}."
            else:
                status_reason = f"Waiting for source task {handoff['from_task_id']} to finish successfully."
        else:
            missing_deliverables = missing_handoff_deliverables(project_root, handoff, report)
            if missing_deliverables:
                status = HANDOFF_BLOCKED
                status_reason = "Missing required handoff deliverables."
            else:
                status = HANDOFF_SATISFIED
                status_reason = f"Source task {handoff['from_task_id']} satisfied the handoff."
                satisfied_by_task_ids = [handoff["from_task_id"]]

    updated = dict(handoff)
    updated["status"] = status
    updated["status_reason"] = status_reason
    updated["satisfied_by_task_ids"] = satisfied_by_task_ids
    updated["missing_deliverables"] = missing_deliverables
    updated["last_checked_at"] = now_timestamp()
    if not updated.get("to_task_ids"):
        updated["to_task_ids"] = derive_target_tasks(handoff, tasks_by_id)
    validate_document(updated, "collaboration-handoff.v1", project_root)
    write_json(run_dir / "collaboration-plans" / f"{handoff['handoff_id']}.json", updated)

    if status != previous_status or status_reason != previous_reason:
        record_event(
            project_root,
            run_id,
            phase=handoff["phase"],
            activity_id=handoff["from_task_id"],
            event_type="handoff.status_updated",
            message=f"Handoff {handoff['handoff_id']} is now {status}.",
            payload={
                "handoff_id": handoff["handoff_id"],
                "status": status,
                "status_reason": status_reason,
                "to_task_ids": list(updated.get("to_task_ids", [])),
                "source_execution_path": (
                    str(source_execution_path.relative_to(project_root)) if source_execution_path.exists() else None
                ),
            },
        )
    return updated


def handoff_path(run_dir: Path, handoff_id: str) -> Path:
    return run_dir / "collaboration-plans" / f"{handoff_id}.json"


def derive_target_tasks(handoff: dict[str, Any], tasks_by_id: dict[str, dict[str, Any]]) -> list[str]:
    target_ids: list[str] = []
    shared_asset_ids = set(handoff.get("shared_asset_ids", []))
    handoff_objective_id = handoff.get("objective_id")
    for task_id, task in tasks_by_id.items():
        if task_id == handoff["from_task_id"]:
            continue
        task_objective_id = task.get("objective_id")
        if handoff_objective_id is not None and task_objective_id is not None and task_objective_id != handoff_objective_id:
            continue
        if task.get("capability") != handoff["to_capability"]:
            continue
        if shared_asset_ids and shared_asset_ids.intersection(set(task.get("shared_asset_ids", []))):
            target_ids.append(task_id)
            continue
        if handoff["from_task_id"] in task.get("depends_on", []):
            target_ids.append(task_id)
            continue
        inputs = {str(value) for value in task.get("inputs", [])}
        if f"Output of {handoff['from_task_id']}" in inputs or f"Outputs from {handoff['from_task_id']}" in inputs:
            target_ids.append(task_id)
    return sorted(set(target_ids))


def blocking_handoffs_for_task(task: dict[str, Any], handoffs_by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    declared = [value for value in task.get("handoff_dependencies", []) if isinstance(value, str)]
    if declared:
        return [
            handoffs_by_id[handoff_id]
            for handoff_id in declared
            if handoff_id in handoffs_by_id and handoffs_by_id[handoff_id].get("blocking", False)
        ]
    inferred: list[dict[str, Any]] = []
    task_shared_assets = set(task.get("shared_asset_ids", []))
    for handoff in handoffs_by_id.values():
        if not handoff.get("blocking", False):
            continue
        if handoff["objective_id"] != task["objective_id"]:
            continue
        if task["task_id"] == handoff["from_task_id"]:
            continue
        if handoff.get("to_task_ids") and task["task_id"] in handoff["to_task_ids"]:
            inferred.append(handoff)
            continue
        if handoff["to_capability"] != task.get("capability"):
            continue
        if task_shared_assets and task_shared_assets.intersection(set(handoff.get("shared_asset_ids", []))):
            inferred.append(handoff)
    return inferred


def handoff_status_counts(handoffs: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "satisfied": sum(1 for handoff in handoffs if handoff.get("status") == HANDOFF_SATISFIED),
        "pending": sum(1 for handoff in handoffs if handoff.get("status") in PENDING_HANDOFF_STATUSES),
        "blocked": sum(1 for handoff in handoffs if handoff.get("status") == HANDOFF_BLOCKED),
    }


def missing_handoff_deliverables(project_root: Path, handoff: dict[str, Any], report: dict[str, Any]) -> list[str]:
    report_artifacts = {artifact.get("path") for artifact in report.get("artifacts", []) if artifact.get("path")}
    missing: list[str] = []
    for deliverable in handoff.get("deliverables", []):
        if not is_path_like_deliverable(deliverable):
            continue
        if deliverable in report_artifacts:
            continue
        if (project_root / deliverable).exists():
            continue
        missing.append(deliverable)
    return missing


def is_path_like_deliverable(deliverable: str) -> bool:
    path = Path(deliverable)
    return "/" in deliverable or "\\" in deliverable or bool(path.suffix)
