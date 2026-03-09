from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .filesystem import append_jsonl, ensure_dir, load_optional_json, read_json, write_json_atomic
from .schemas import validate_document

PROGRESS_BY_STAGE = {
    "waiting_dependencies": 0.0,
    "queued": 0.1,
    "prompt_rendered": 0.2,
    "launching": 0.3,
    "running": 0.6,
    "finalizing": 0.85,
    "ready_for_bundle_review": 1.0,
    "blocked": 1.0,
    "needs_revision": 1.0,
    "completed": 1.0,
    "failed": 1.0,
    "accepted": 1.0,
    "rejected": 1.0,
    "skipped_existing": 1.0,
}

TERMINAL_ACTIVITY_STATUSES = {
    "ready_for_bundle_review",
    "blocked",
    "needs_revision",
    "completed",
    "failed",
    "accepted",
    "rejected",
    "skipped_existing",
}


def now_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def initialize_live_run(project_root: Path, run_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    ensure_dir(run_dir / "live" / "activities")
    phase_plan = read_json(run_dir / "phase-plan.json")
    payload = {
        "schema": "run-live-state.v1",
        "run_id": run_id,
        "current_phase": phase_plan["current_phase"],
        "active_activity_ids": [],
        "queued_activity_ids": [],
        "counts_by_status": {},
        "counts_by_kind": {},
        "updated_at": now_timestamp(),
    }
    validate_document(payload, "run-live-state.v1", project_root)
    write_json_atomic(run_dir / "live" / "run-state.json", payload)
    return payload


def plan_activity_id(phase: str, objective_id: str) -> str:
    return f"plan:{phase}:{objective_id}"


def activity_path(run_dir: Path, activity_id: str) -> Path:
    return run_dir / "live" / "activities" / f"{activity_id.replace(':', '__')}.json"


def ensure_activity(
    project_root: Path,
    run_id: str,
    *,
    activity_id: str,
    kind: str,
    entity_id: str,
    phase: str,
    objective_id: str,
    display_name: str,
    assigned_role: str | None,
    status: str,
    progress_stage: str | None = None,
    progress_fraction: float | None = None,
    queue_position: int | None = None,
    runner_id: str | None = None,
    current_activity: str | None = None,
    prompt_path: str | None = None,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
    output_path: str | None = None,
    dependency_blockers: list[str] | None = None,
    latest_event: dict[str, str] | None = None,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    ensure_dir(run_dir / "live" / "activities")
    activity_file = activity_path(run_dir, activity_id)
    existing = load_optional_json(activity_file)
    started_at = existing["started_at"] if existing else now_timestamp()
    payload = {
        "schema": "activity-live-state.v1",
        "run_id": run_id,
        "activity_id": activity_id,
        "kind": kind,
        "entity_id": entity_id,
        "phase": phase,
        "objective_id": objective_id,
        "display_name": display_name,
        "assigned_role": assigned_role,
        "status": status,
        "progress_stage": progress_stage or status,
        "progress_fraction": progress_fraction if progress_fraction is not None else progress_for_stage(progress_stage or status),
        "queue_position": queue_position,
        "runner_id": runner_id,
        "current_activity": current_activity,
        "prompt_path": prompt_path,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "output_path": output_path,
        "dependency_blockers": dependency_blockers or [],
        "started_at": started_at,
        "updated_at": now_timestamp(),
        "latest_event": latest_event,
    }
    validate_document(payload, "activity-live-state.v1", project_root)
    write_json_atomic(activity_file, payload)
    refresh_run_state(project_root, run_id)
    return payload


def update_activity(
    project_root: Path,
    run_id: str,
    activity_id: str,
    **updates: Any,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    payload = read_json(activity_path(run_dir, activity_id))
    payload.update({key: value for key, value in updates.items() if value is not None or key in updates})
    payload["updated_at"] = now_timestamp()
    if "status" in updates and "progress_stage" not in updates:
        payload["progress_stage"] = payload["status"]
    progress_stage = payload.get("progress_stage") or payload["status"]
    payload["progress_stage"] = progress_stage
    payload["progress_fraction"] = updates.get("progress_fraction", progress_for_stage(progress_stage))
    validate_document(payload, "activity-live-state.v1", project_root)
    write_json_atomic(activity_path(run_dir, activity_id), payload)
    refresh_run_state(project_root, run_id)
    return payload


def record_event(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
    activity_id: str | None = None,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    event = {
        "schema": "live-event.v1",
        "timestamp": now_timestamp(),
        "run_id": run_id,
        "phase": phase,
        "activity_id": activity_id,
        "event_type": event_type,
        "message": message,
        "payload": payload or {},
    }
    validate_document(event, "live-event.v1", project_root)
    append_jsonl(run_dir / "live" / "events.jsonl", event)
    if activity_id:
        activity_file = activity_path(run_dir, activity_id)
        if activity_file.exists():
            activity = read_json(activity_file)
            activity["latest_event"] = {
                "timestamp": event["timestamp"],
                "event_type": event_type,
                "message": message,
            }
            activity["updated_at"] = event["timestamp"]
            validate_document(activity, "activity-live-state.v1", project_root)
            write_json_atomic(activity_file, activity)
    refresh_run_state(project_root, run_id)
    return event


def refresh_run_state(project_root: Path, run_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    ensure_dir(run_dir / "live" / "activities")
    phase_plan = read_json(run_dir / "phase-plan.json")
    current_phase = phase_plan["current_phase"]
    activities = list_activities(project_root, run_id, phase=current_phase)
    active_activity_ids = [
        activity["activity_id"]
        for activity in activities
        if activity["status"] not in TERMINAL_ACTIVITY_STATUSES and activity["status"] not in {"queued", "waiting_dependencies"}
    ]
    queued_activity_ids = [
        activity["activity_id"]
        for activity in activities
        if activity["status"] in {"queued", "waiting_dependencies"}
    ]
    counts_by_status = dict(Counter(activity["status"] for activity in activities))
    counts_by_kind = dict(Counter(activity["kind"] for activity in activities))
    payload = {
        "schema": "run-live-state.v1",
        "run_id": run_id,
        "current_phase": current_phase,
        "active_activity_ids": active_activity_ids,
        "queued_activity_ids": queued_activity_ids,
        "counts_by_status": counts_by_status,
        "counts_by_kind": counts_by_kind,
        "updated_at": now_timestamp(),
    }
    validate_document(payload, "run-live-state.v1", project_root)
    write_json_atomic(run_dir / "live" / "run-state.json", payload)
    return payload


def list_activities(project_root: Path, run_id: str, *, phase: str | None = None) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    activities = []
    for path in sorted((run_dir / "live" / "activities").glob("*.json")):
        payload = read_json(path)
        if phase is None or payload["phase"] == phase:
            activities.append(payload)
    return activities


def read_run_state(project_root: Path, run_id: str) -> dict[str, Any]:
    run_state_path = project_root / "runs" / run_id / "live" / "run-state.json"
    if not run_state_path.exists():
        return initialize_live_run(project_root, run_id)
    return read_json(run_state_path)


def read_activity(project_root: Path, run_id: str, activity_id: str) -> dict[str, Any]:
    return read_json(activity_path(project_root / "runs" / run_id, activity_id))


def read_events(project_root: Path, run_id: str, *, activity_id: str | None = None) -> list[dict[str, Any]]:
    events_path = project_root / "runs" / run_id / "live" / "events.jsonl"
    if not events_path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        event = read_json_line(stripped)
        if activity_id is None or event["activity_id"] == activity_id:
            events.append(event)
    return events


def read_json_line(line: str) -> dict[str, Any]:
    import json

    return json.loads(line)


def progress_for_stage(stage: str) -> float:
    return PROGRESS_BY_STAGE.get(stage, PROGRESS_BY_STAGE.get("running", 0.6))
