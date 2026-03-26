from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .filesystem import read_json, write_json
from .live import now_timestamp, record_event
from .output_descriptors import (
    descriptor_output_id,
    descriptor_path,
    normalize_output_descriptors,
    output_descriptor_ids,
    repo_relative_path_exists,
)
from .schemas import validate_document
from .worktree_manager import integration_workspace_path


HANDOFF_PLANNED = "planned"
HANDOFF_WAITING_ON_SOURCE = "waiting_on_source"
HANDOFF_SATISFIED = "satisfied"
HANDOFF_BLOCKED = "blocked"
PENDING_HANDOFF_STATUSES = {HANDOFF_PLANNED, HANDOFF_WAITING_ON_SOURCE}


def normalize_handoff_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["deliverables"] = normalize_output_descriptors(list(payload.get("deliverables", [])))
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
                satisfied_by_task_ids = contributing_handoff_task_ids(project_root, handoff)

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
    allow_shared_asset_inference = handoff.get("to_capability") != handoff.get("from_capability")
    for task_id, task in tasks_by_id.items():
        if task_id == handoff["from_task_id"]:
            continue
        task_objective_id = task.get("objective_id")
        if handoff_objective_id is not None and task_objective_id is not None and task_objective_id != handoff_objective_id:
            continue
        if task.get("capability") != handoff["to_capability"]:
            continue
        # Same-capability handoffs often represent later synthesis or review work inside
        # the lane. Inferring targets from a generic shared asset creates cycles, so only
        # use shared-asset inference for cross-capability handoffs. Same-capability
        # handoffs still target downstream tasks via explicit depends_on/input links.
        if allow_shared_asset_inference and shared_asset_ids and shared_asset_ids.intersection(
            set(task.get("shared_asset_ids", []))
        ):
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
        if handoff.get("to_capability") == handoff.get("from_capability"):
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


def normalized_deliverable_values(values: list[Any]) -> set[str]:
    return set(output_descriptor_ids(values))


def missing_handoff_deliverables(project_root: Path, handoff: dict[str, Any], report: dict[str, Any]) -> list[str]:
    search_roots = deliverable_search_roots(project_root, handoff)
    produced_outputs = {
        descriptor_output_id(item): item
        for item in normalize_output_descriptors(list(report.get("produced_outputs", [])))
    }
    passed_validation_ids = {
        str(item.get("id"))
        for item in report.get("validation_results", [])
        if isinstance(item, dict) and item.get("status") == "passed" and isinstance(item.get("id"), str)
    }
    missing: list[str] = []
    for deliverable in normalize_output_descriptors(list(handoff.get("deliverables", []))):
        output_id = descriptor_output_id(deliverable)
        produced = produced_outputs.get(output_id)
        if produced is not None and produced_output_is_satisfied(produced, passed_validation_ids=passed_validation_ids, search_roots=search_roots):
            continue
        missing.append(output_id)
    return missing


def produced_output_is_satisfied(
    produced: dict[str, Any],
    *,
    passed_validation_ids: set[str],
    search_roots: list[Path],
) -> bool:
    kind = str(produced.get("kind"))
    if kind in {"artifact", "asset"}:
        path = descriptor_path(produced)
        return bool(path and repo_relative_path_exists(search_roots, path))
    if kind == "assertion":
        evidence = produced.get("evidence", {})
        if not isinstance(evidence, dict):
            return False
        validation_ids = [
            str(item).strip()
            for item in evidence.get("validation_ids", [])
            if isinstance(item, str) and item.strip()
        ]
        if any(validation_id not in passed_validation_ids for validation_id in validation_ids):
            return False
        artifact_paths = [
            str(item).strip()
            for item in evidence.get("artifact_paths", [])
            if isinstance(item, str) and item.strip()
        ]
        return all(repo_relative_path_exists(search_roots, artifact_path) for artifact_path in artifact_paths)
    return False


def deliverable_search_roots(project_root: Path, handoff: dict[str, Any]) -> list[Path]:
    run_id = str(handoff["run_id"])
    roots: list[Path] = [project_root]
    integration_workspace = integration_workspace_path(project_root, run_id)
    if integration_workspace.exists():
        roots.append(integration_workspace)
    execution_path = project_root / "runs" / run_id / "executions" / f"{handoff['from_task_id']}.json"
    if execution_path.exists():
        execution = read_json(execution_path)
        workspace_path = execution.get("workspace_path")
        if isinstance(workspace_path, str) and workspace_path.strip():
            workspace = Path(workspace_path)
            if not workspace.is_absolute():
                workspace = (project_root / workspace).resolve()
            if workspace.exists():
                roots.append(workspace)
    unique_roots: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique_roots.append(root)
    return unique_roots


def resolve_deliverable_path(search_roots: list[Path], deliverable: str) -> Path | None:
    path = Path(deliverable)
    if path.is_absolute():
        return path if path.exists() else None
    for root in search_roots:
        candidate = (root / path).resolve()
        if candidate.exists():
            return candidate
    return None


def collect_handoff_artifact_paths(project_root: Path, handoff: dict[str, Any]) -> dict[str, Path]:
    desired_deliverables = normalize_output_descriptors(list(handoff.get("deliverables", [])))
    artifact_paths: dict[str, Path] = {}
    search_roots = deliverable_search_roots(project_root, handoff)
    for deliverable in desired_deliverables:
        path_value = descriptor_path(deliverable)
        if not path_value:
            continue
        resolved = resolve_deliverable_path(search_roots, path_value)
        if resolved is not None:
            artifact_paths.setdefault(path_value, resolved)
    return artifact_paths


def contributing_handoff_task_ids(project_root: Path, handoff: dict[str, Any]) -> list[str]:
    return [handoff["from_task_id"]]
