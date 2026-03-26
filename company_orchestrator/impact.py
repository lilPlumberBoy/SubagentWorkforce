from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .filesystem import ensure_dir, load_optional_json, read_json, write_json
from .handoffs import derive_target_tasks, list_handoffs, normalize_handoff_payload
from .input_lineage import producer_output_records, referenced_task_output_id
from .live import (
    TERMINAL_ACTIVITY_STATUSES,
    activity_path,
    append_activity_warning,
    ensure_activity,
    list_activities,
    now_timestamp,
    read_activity,
    record_event,
    update_activity,
)
from .output_descriptors import descriptor_output_id, normalize_output_descriptors
from .schemas import validate_document


ACTIVE_ACTIVITY_STATUSES = {
    "prompt_rendered",
    "launching",
    "running",
    "recovering",
    "finalizing",
}


def build_change_impact_graph(project_root: Path, run_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    tasks_by_id: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "tasks").glob("*.json")):
        task = read_json(path)
        tasks_by_id[task["task_id"]] = task

    handoffs_by_id: dict[str, dict[str, Any]] = {}
    consumers_by_output_id: dict[str, set[str]] = defaultdict(set)
    consumers_by_handoff_id: dict[str, set[str]] = defaultdict(set)
    task_consumers: dict[str, set[str]] = defaultdict(set)

    producers_by_output_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    producer_records_by_path: dict[str, list[dict[str, str]]] = defaultdict(list)
    producer_records_by_report_path: dict[str, list[dict[str, str]]] = defaultdict(list)
    for task_id, task in tasks_by_id.items():
        output_records = producer_output_records(project_root, run_id, task)
        report_rel_path = str((run_dir / "reports" / f"{task_id}.json").relative_to(project_root))
        for record in output_records:
            producers_by_output_id[record["output_id"]].append(
                {
                    "task_id": task_id,
                    "objective_id": task["objective_id"],
                    "phase": task["phase"],
                }
            )
            producer_records_by_report_path[report_rel_path].append(record)
            artifact_path = record.get("path")
            if artifact_path:
                producer_records_by_path[artifact_path].append(record)

    for handoff in list_handoffs(run_dir):
        normalized = normalize_handoff_payload(handoff)
        if not normalized.get("to_task_ids"):
            normalized["to_task_ids"] = derive_target_tasks(normalized, tasks_by_id)
        handoffs_by_id[normalized["handoff_id"]] = normalized
        target_ids = {task_id for task_id in normalized.get("to_task_ids", []) if task_id in tasks_by_id}
        consumers_by_handoff_id[normalized["handoff_id"]].update(target_ids)
        if normalized.get("from_task_id") in tasks_by_id:
            task_consumers[normalized["from_task_id"]].update(target_ids)
        for deliverable in normalize_output_descriptors(list(normalized.get("deliverables", [])), allow_legacy_strings=False):
            consumers_by_output_id[descriptor_output_id(deliverable)].update(target_ids)

    for task_id, task in tasks_by_id.items():
        referenced_task_ids = set(str(value) for value in task.get("depends_on", []) if isinstance(value, str))
        for input_ref in task.get("inputs", []):
            if not isinstance(input_ref, str):
                continue
            normalized_input_ref = input_ref.strip()
            if not normalized_input_ref:
                continue
            referenced_task_id = referenced_task_output_id(normalized_input_ref)
            if referenced_task_id is not None:
                referenced_task_ids.add(referenced_task_id)
                continue
            for record in producer_records_by_path.get(normalized_input_ref, []):
                if record["task_id"] == task_id:
                    continue
                consumers_by_output_id[record["output_id"]].add(task_id)
                task_consumers[record["task_id"]].add(task_id)
            for record in producer_records_by_report_path.get(normalized_input_ref, []):
                if record["task_id"] == task_id:
                    continue
                consumers_by_output_id[record["output_id"]].add(task_id)
                task_consumers[record["task_id"]].add(task_id)
        for handoff_id in [value for value in task.get("handoff_dependencies", []) if isinstance(value, str)]:
            handoff = handoffs_by_id.get(handoff_id)
            if handoff is not None:
                referenced_task_ids.add(handoff["from_task_id"])
                consumers_by_handoff_id[handoff_id].add(task_id)
        for referenced_task_id in referenced_task_ids:
            task_consumers[referenced_task_id].add(task_id)

    return {
        "tasks_by_id": tasks_by_id,
        "handoffs_by_id": handoffs_by_id,
        "producers_by_output_id": {key: sorted(value, key=lambda item: item["task_id"]) for key, value in producers_by_output_id.items()},
        "consumers_by_output_id": {key: sorted(value) for key, value in consumers_by_output_id.items()},
        "consumers_by_handoff_id": {key: sorted(value) for key, value in consumers_by_handoff_id.items()},
        "task_consumers": {key: sorted(value) for key, value in task_consumers.items()},
    }


def report_revision_for_task(project_root: Path, run_id: str, task_id: str) -> dict[str, Any] | None:
    report_path = project_root / "runs" / run_id / "reports" / f"{task_id}.json"
    if not report_path.exists():
        return None
    report = read_json(report_path)
    revision_hash = hashlib.sha1(
        json.dumps(
            {
                "status": report.get("status"),
                "produced_outputs": report.get("produced_outputs", []),
                "summary": report.get("summary"),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "report_path": str(report_path.relative_to(project_root)),
        "report_revision": revision_hash,
        "produced_outputs": [
            descriptor_output_id(item)
            for item in normalize_output_descriptors(list(report.get("produced_outputs", [])), allow_legacy_strings=False)
        ],
    }


def analyze_change_request_impact(
    project_root: Path,
    run_id: str,
    change_request: dict[str, Any],
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    graph = graph or build_change_impact_graph(project_root, run_id)
    tasks_by_id = graph["tasks_by_id"]
    producers_by_output_id = graph["producers_by_output_id"]
    consumers_by_output_id = graph["consumers_by_output_id"]
    consumers_by_handoff_id = graph["consumers_by_handoff_id"]
    task_consumers = graph["task_consumers"]

    direct_tasks: set[str] = set()
    source_revisions: list[dict[str, Any]] = []

    for output_id in change_request.get("affected_output_ids", []):
        direct_tasks.update(consumers_by_output_id.get(output_id, []))
        for producer in producers_by_output_id.get(output_id, []):
            direct_tasks.update(task_consumers.get(producer["task_id"], []))
            revision = report_revision_for_task(project_root, run_id, producer["task_id"])
            source_revisions.append(
                {
                    "output_id": output_id,
                    "handoff_id": None,
                    "producer_task_id": producer["task_id"],
                    "producer_objective_id": producer["objective_id"],
                    "producer_phase": producer["phase"],
                    "report_path": revision["report_path"] if revision is not None else None,
                    "report_revision": revision["report_revision"] if revision is not None else None,
                }
            )

    for handoff_id in change_request.get("affected_handoff_ids", []):
        direct_tasks.update(consumers_by_handoff_id.get(handoff_id, []))
        handoff = graph["handoffs_by_id"].get(handoff_id)
        if handoff is None:
            continue
        revision = report_revision_for_task(project_root, run_id, handoff["from_task_id"])
        source_revisions.append(
            {
                "output_id": None,
                "handoff_id": handoff_id,
                "producer_task_id": handoff["from_task_id"],
                "producer_objective_id": handoff["objective_id"],
                "producer_phase": handoff["phase"],
                "report_path": revision["report_path"] if revision is not None else None,
                "report_revision": revision["report_revision"] if revision is not None else None,
            }
        )

    impacted_task_ids: list[str] = []
    queue = deque(sorted(task_id for task_id in direct_tasks if task_id in tasks_by_id))
    seen: set[str] = set()
    while queue:
        task_id = queue.popleft()
        if task_id in seen:
            continue
        seen.add(task_id)
        impacted_task_ids.append(task_id)
        for consumer_task_id in task_consumers.get(task_id, []):
            if consumer_task_id not in seen:
                queue.append(consumer_task_id)

    impacted_objective_ids = sorted({tasks_by_id[task_id]["objective_id"] for task_id in impacted_task_ids if task_id in tasks_by_id})
    direct_objective_ids = sorted({tasks_by_id[task_id]["objective_id"] for task_id in direct_tasks if task_id in tasks_by_id})

    notifications: list[dict[str, Any]] = []
    for task_id in impacted_task_ids:
        task = tasks_by_id[task_id]
        activity_file = activity_path(project_root / "runs" / run_id, task_id)
        existing = load_optional_json(activity_file)
        existing_status = existing.get("status") if isinstance(existing, dict) else None
        action = "pause" if existing_status in ACTIVE_ACTIVITY_STATUSES else "replan"
        notifications.append(
            {
                "task_id": task_id,
                "objective_id": task["objective_id"],
                "phase": task["phase"],
                "activity_id": task_id,
                "action_required": action,
            }
        )

    impact_record = {
        "schema": "change-impact.v1",
        "run_id": run_id,
        "change_id": change_request["change_id"],
        "source_task_id": change_request["source_task_id"],
        "source_objective_id": change_request["source_objective_id"],
        "phase": change_request["phase"],
        "affected_output_ids": list(change_request.get("affected_output_ids", [])),
        "affected_handoff_ids": list(change_request.get("affected_handoff_ids", [])),
        "source_revisions": source_revisions,
        "directly_impacted_task_ids": sorted(task_id for task_id in direct_tasks if task_id in tasks_by_id),
        "directly_impacted_objective_ids": direct_objective_ids,
        "impacted_task_ids": impacted_task_ids,
        "impacted_objective_ids": impacted_objective_ids,
        "notifications": notifications,
        "updated_at": now_timestamp(),
    }
    validate_document(impact_record, "change-impact.v1", project_root)
    return impact_record


def persist_change_impact(project_root: Path, run_id: str, impact_record: dict[str, Any]) -> dict[str, Any]:
    impact_dir = ensure_dir(project_root / "runs" / run_id / "change-impacts")
    write_json(impact_dir / f"{impact_record['change_id']}.json", impact_record)
    return impact_record


def active_change_impacts(project_root: Path, run_id: str) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    impact_dir = run_dir / "change-impacts"
    if not impact_dir.exists():
        return []
    impacts: list[dict[str, Any]] = []
    for path in sorted(impact_dir.glob("*.json")):
        payload = read_json(path)
        request_path = run_dir / "change-requests" / f"{payload['change_id']}.json"
        if request_path.exists():
            request = read_json(request_path)
            if request.get("replacement_plan_revision") is not None:
                continue
        impacts.append(payload)
    return impacts


def stale_task_notifications(project_root: Path, run_id: str, *, phase: str | None = None) -> dict[str, list[dict[str, Any]]]:
    notifications: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for impact in active_change_impacts(project_root, run_id):
        for item in impact.get("notifications", []):
            if phase is not None and item.get("phase") != phase:
                continue
            notifications[item["task_id"]].append(
                {
                    "change_id": impact["change_id"],
                    "action_required": item["action_required"],
                    "reason": f"Approved change request {impact['change_id']} invalidated consumed inputs.",
                }
            )
    return dict(notifications)


def apply_approved_change_impacts(
    project_root: Path,
    run_id: str,
    change_request_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    request_dir = run_dir / "change-requests"
    if not request_dir.exists():
        return []
    graph = build_change_impact_graph(project_root, run_id)
    selected_ids = set(change_request_ids or [])
    impacted_records: list[dict[str, Any]] = []
    for path in sorted(request_dir.glob("*.json")):
        request = read_json(path)
        if selected_ids and request["change_id"] not in selected_ids:
            continue
        if request.get("approval", {}).get("status") != "approved":
            continue
        impact_record = analyze_change_request_impact(project_root, run_id, request, graph=graph)
        persist_change_impact(project_root, run_id, impact_record)
        request["impacted_objective_ids"] = impact_record["impacted_objective_ids"]
        request["impacted_task_ids"] = impact_record["impacted_task_ids"]
        validate_document(request, "change-request.v2", project_root)
        write_json(path, request)
        notify_impacted_tasks(project_root, run_id, request, impact_record, graph["tasks_by_id"])
        record_event(
            project_root,
            run_id,
            phase=request["phase"],
            activity_id=None,
            event_type="change.impact_resolved",
            message=f"Approved change request {request['change_id']} impacts {len(impact_record['impacted_task_ids'])} tasks.",
            payload={
                "change_id": request["change_id"],
                "impacted_task_ids": impact_record["impacted_task_ids"],
                "impacted_objective_ids": impact_record["impacted_objective_ids"],
            },
        )
        impacted_records.append(impact_record)
    return impacted_records


def notify_impacted_tasks(
    project_root: Path,
    run_id: str,
    change_request: dict[str, Any],
    impact_record: dict[str, Any],
    tasks_by_id: dict[str, dict[str, Any]],
) -> None:
    run_dir = project_root / "runs" / run_id
    for notification in impact_record.get("notifications", []):
        task_id = notification["task_id"]
        task = tasks_by_id[task_id]
        reason = f"Approved change request {change_request['change_id']} invalidated consumed inputs."
        activity_file = activity_path(run_dir, task_id)
        existing = load_optional_json(activity_file)
        if existing is None:
            ensure_activity(
                project_root,
                run_id,
                activity_id=task_id,
                kind="task_execution",
                entity_id=task_id,
                phase=task["phase"],
                objective_id=task["objective_id"],
                display_name=task_id,
                assigned_role=task["assigned_role"],
                status="needs_revision",
                progress_stage="needs_revision",
                current_activity="Inputs stale after approved change request.",
                prompt_path=None,
                stdout_path=f"runs/{run_id}/executions/{task_id}.stdout.jsonl",
                stderr_path=f"runs/{run_id}/executions/{task_id}.stderr.log",
                output_path=f"runs/{run_id}/reports/{task_id}.json",
                dependency_blockers=[],
                status_reason=reason,
            )
        else:
            activity = read_activity(project_root, run_id, task_id)
            if activity["status"] in ACTIVE_ACTIVITY_STATUSES:
                append_activity_warning(
                    project_root,
                    run_id,
                    task_id,
                    code="change_impact",
                    message=reason,
                )
                update_activity(
                    project_root,
                    run_id,
                    task_id,
                    status_reason=reason,
                )
            else:
                update_activity(
                    project_root,
                    run_id,
                    task_id,
                    status="needs_revision",
                    progress_stage="needs_revision",
                    current_activity="Inputs stale after approved change request.",
                    status_reason=reason,
                )
        record_event(
            project_root,
            run_id,
            phase=task["phase"],
            activity_id=task_id,
            event_type="task.change_impact",
            message=f"Task {task_id} must {notification['action_required']} after approved change request {change_request['change_id']}.",
            payload={
                "change_id": change_request["change_id"],
                "action_required": notification["action_required"],
                "objective_id": task["objective_id"],
            },
        )
    for objective_id in impact_record.get("impacted_objective_ids", []):
        record_event(
            project_root,
            run_id,
            phase=change_request["phase"],
            activity_id=None,
            event_type="objective.change_impact",
            message=f"Objective {objective_id} has impacted work after approved change request {change_request['change_id']}.",
            payload={
                "change_id": change_request["change_id"],
                "objective_id": objective_id,
            },
        )
