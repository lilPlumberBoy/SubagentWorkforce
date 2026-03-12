from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any

from .filesystem import append_jsonl, ensure_dir, load_optional_json, read_json, write_json_atomic
from .objective_roots import find_objective_app_root
from .schemas import validate_document

PROGRESS_BY_STAGE = {
    "waiting_dependencies": 0.0,
    "queued": 0.1,
    "prompt_rendered": 0.2,
    "launching": 0.3,
    "running": 0.6,
    "recovering": 0.7,
    "finalizing": 0.85,
    "ready_for_bundle_review": 1.0,
    "blocked": 1.0,
    "needs_revision": 1.0,
    "completed": 1.0,
    "failed": 1.0,
    "interrupted": 1.0,
    "recovered": 1.0,
    "abandoned": 1.0,
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
    "interrupted",
    "recovered",
    "abandoned",
    "accepted",
    "rejected",
    "skipped_existing",
}

HISTORY_ACTIVITY_STATUSES = TERMINAL_ACTIVITY_STATUSES | {"ready_for_bundle_review"}

_RUN_LOCKS: dict[str, threading.RLock] = {}
_RUN_LOCKS_GUARD = threading.Lock()


def run_lock(run_id: str) -> threading.RLock:
    with _RUN_LOCKS_GUARD:
        lock = _RUN_LOCKS.get(run_id)
        if lock is None:
            lock = threading.RLock()
            _RUN_LOCKS[run_id] = lock
        return lock


def now_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def initialize_live_run(project_root: Path, run_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    with run_lock(run_id):
        ensure_dir(run_dir / "live" / "activities")
        run_state_path = run_dir / "live" / "run-state.json"
        if run_state_path.exists():
            return refresh_run_state(project_root, run_id)
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
        write_json_atomic(run_state_path, payload)
        return payload


def plan_activity_id(phase: str, objective_id: str) -> str:
    return f"plan:{phase}:{objective_id}"


def capability_plan_activity_id(phase: str, objective_id: str, capability: str) -> str:
    return f"plan:{phase}:{objective_id}:{capability}"


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
    warnings: list[dict[str, str]] | None = None,
    parallel_execution_requested: bool | None = None,
    parallel_execution_granted: bool | None = None,
    parallel_fallback_reason: str | None = None,
    workspace_path: str | None = None,
    branch_name: str | None = None,
    dependency_blocker_fingerprint: str | None = None,
    handoff_blocker_fingerprint: str | None = None,
    attempt: int | None = None,
    status_reason: str | None = None,
    interrupted_at: str | None = None,
    recovered_at: str | None = None,
    recovery_action: str | None = None,
    superseded_by: str | None = None,
    process_metadata: dict[str, Any] | None = None,
    artifact_reconciliation: dict[str, Any] | None = None,
    begin_attempt: bool = False,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    with run_lock(run_id):
        ensure_dir(run_dir / "live" / "activities")
        activity_file = activity_path(run_dir, activity_id)
        existing_payload = load_optional_json(activity_file)
        existing = normalize_activity_payload(existing_payload) if existing_payload is not None else None
        started_at = existing["started_at"] if existing else now_timestamp()
        attempt_value = int(existing.get("attempt", 1)) if existing else 1
        if begin_attempt:
            attempt_value = (int(existing.get("attempt", 1)) + 1) if existing else 1
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
            "attempt": attempt if attempt is not None else attempt_value,
            "status_reason": status_reason if status_reason is not None else (existing.get("status_reason") if existing else None),
            "interrupted_at": interrupted_at if interrupted_at is not None else (existing.get("interrupted_at") if existing else None),
            "recovered_at": recovered_at if recovered_at is not None else (existing.get("recovered_at") if existing else None),
            "recovery_action": recovery_action if recovery_action is not None else (existing.get("recovery_action") if existing else None),
            "superseded_by": superseded_by if superseded_by is not None else (existing.get("superseded_by") if existing else None),
            "process_metadata": process_metadata if process_metadata is not None else (existing.get("process_metadata") if existing else None),
            "artifact_reconciliation": (
                artifact_reconciliation
                if artifact_reconciliation is not None
                else (existing.get("artifact_reconciliation") if existing else None)
            ),
            "warnings": warnings if warnings is not None else (existing.get("warnings", []) if existing else []),
            "parallel_execution_requested": (
                parallel_execution_requested
                if parallel_execution_requested is not None
                else (existing.get("parallel_execution_requested", False) if existing else False)
            ),
            "parallel_execution_granted": (
                parallel_execution_granted
                if parallel_execution_granted is not None
                else (existing.get("parallel_execution_granted", False) if existing else False)
            ),
            "parallel_fallback_reason": (
                parallel_fallback_reason
                if parallel_fallback_reason is not None
                else (existing.get("parallel_fallback_reason") if existing else None)
            ),
            "workspace_path": workspace_path if workspace_path is not None else (existing.get("workspace_path") if existing else None),
            "branch_name": branch_name if branch_name is not None else (existing.get("branch_name") if existing else None),
            "dependency_blocker_fingerprint": (
                dependency_blocker_fingerprint
                if dependency_blocker_fingerprint is not None
                else (existing.get("dependency_blocker_fingerprint") if existing else None)
            ),
            "handoff_blocker_fingerprint": (
                handoff_blocker_fingerprint
                if handoff_blocker_fingerprint is not None
                else (existing.get("handoff_blocker_fingerprint") if existing else None)
            ),
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
        record_history_transition(project_root, run_id, payload, existing)
        refresh_run_state(project_root, run_id)
        return payload


def update_activity(
    project_root: Path,
    run_id: str,
    activity_id: str,
    **updates: Any,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    with run_lock(run_id):
        previous = normalize_activity_payload(read_json(activity_path(run_dir, activity_id)))
        payload = dict(previous)
        payload.update({key: value for key, value in updates.items() if value is not None or key in updates})
        payload["updated_at"] = now_timestamp()
        if "status" in updates and payload["status"] == "interrupted" and not payload.get("interrupted_at"):
            payload["interrupted_at"] = payload["updated_at"]
        if "status" in updates and payload["status"] == "recovered" and not payload.get("recovered_at"):
            payload["recovered_at"] = payload["updated_at"]
        if "status" in updates and "progress_stage" not in updates:
            payload["progress_stage"] = payload["status"]
        progress_stage = payload.get("progress_stage") or payload["status"]
        payload["progress_stage"] = progress_stage
        payload["progress_fraction"] = updates.get("progress_fraction", progress_for_stage(progress_stage))
        validate_document(payload, "activity-live-state.v1", project_root)
        write_json_atomic(activity_path(run_dir, activity_id), payload)
        record_history_transition(project_root, run_id, payload, previous)
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
    with run_lock(run_id):
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
    with run_lock(run_id):
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


def append_activity_warning(
    project_root: Path,
    run_id: str,
    activity_id: str,
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    with run_lock(run_id):
        payload = normalize_activity_payload(read_json(activity_path(project_root / "runs" / run_id, activity_id)))
        warnings = list(payload.get("warnings", []))
        warning = {"code": code, "message": message}
        if warning not in warnings:
            warnings.append(warning)
        payload["warnings"] = warnings
        payload["updated_at"] = now_timestamp()
        validate_document(payload, "activity-live-state.v1", project_root)
        write_json_atomic(activity_path(project_root / "runs" / run_id, activity_id), payload)
        refresh_run_state(project_root, run_id)
        return payload


def mark_activity_interrupted(
    project_root: Path,
    run_id: str,
    activity_id: str,
    *,
    reason: str,
    artifact_reconciliation: dict[str, Any] | None = None,
    recovery_action: str | None = None,
) -> dict[str, Any]:
    return update_activity(
        project_root,
        run_id,
        activity_id,
        status="interrupted",
        progress_stage="interrupted",
        status_reason=reason,
        interrupted_at=now_timestamp(),
        recovery_action=recovery_action,
        artifact_reconciliation=artifact_reconciliation,
    )


def mark_activity_recovered(
    project_root: Path,
    run_id: str,
    activity_id: str,
    *,
    reason: str,
    recovery_action: str,
    artifact_reconciliation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return update_activity(
        project_root,
        run_id,
        activity_id,
        status="recovered",
        progress_stage="recovered",
        status_reason=reason,
        recovered_at=now_timestamp(),
        recovery_action=recovery_action,
        artifact_reconciliation=artifact_reconciliation,
        current_activity=reason,
    )


def process_alive(process_metadata: dict[str, Any] | None) -> bool:
    if not process_metadata:
        return False
    pid = process_metadata.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import os

        os.kill(pid, 0)
    except OSError:
        return False
    return True


def list_activities(project_root: Path, run_id: str, *, phase: str | None = None) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    activities = []
    for path in sorted((run_dir / "live" / "activities").glob("*.json")):
        payload = normalize_activity_payload(read_json(path))
        if phase is None or payload["phase"] == phase:
            activities.append(payload)
    return activities


def read_run_state(project_root: Path, run_id: str) -> dict[str, Any]:
    run_state_path = project_root / "runs" / run_id / "live" / "run-state.json"
    if not run_state_path.exists():
        return initialize_live_run(project_root, run_id)
    return read_json(run_state_path)


def read_activity(project_root: Path, run_id: str, activity_id: str) -> dict[str, Any]:
    return normalize_activity_payload(read_json(activity_path(project_root / "runs" / run_id, activity_id)))


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


def read_activity_history(project_root: Path, run_id: str) -> list[dict[str, Any]]:
    history_path = project_root / "runs" / run_id / "live" / "activity-history.jsonl"
    if not history_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        entries.append(read_json_line(stripped))
    return entries


def read_json_line(line: str) -> dict[str, Any]:
    import json

    return json.loads(line)


def progress_for_stage(stage: str) -> float:
    return PROGRESS_BY_STAGE.get(stage, PROGRESS_BY_STAGE.get("running", 0.6))


def is_terminal_activity_status(status: str) -> bool:
    return status in TERMINAL_ACTIVITY_STATUSES


def normalize_activity_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    normalized.setdefault("attempt", 1)
    normalized.setdefault("status_reason", None)
    normalized.setdefault("interrupted_at", None)
    normalized.setdefault("recovered_at", None)
    normalized.setdefault("recovery_action", None)
    normalized.setdefault("superseded_by", None)
    normalized.setdefault("process_metadata", None)
    normalized.setdefault("artifact_reconciliation", None)
    normalized.setdefault("warnings", [])
    normalized.setdefault("parallel_execution_requested", False)
    normalized.setdefault("parallel_execution_granted", False)
    normalized.setdefault("parallel_fallback_reason", None)
    normalized.setdefault("workspace_path", None)
    normalized.setdefault("branch_name", None)
    normalized.setdefault("dependency_blocker_fingerprint", None)
    normalized.setdefault("handoff_blocker_fingerprint", None)
    return normalized


def record_history_transition(
    project_root: Path,
    run_id: str,
    payload: dict[str, Any],
    previous: dict[str, Any] | None,
) -> None:
    if payload["status"] not in HISTORY_ACTIVITY_STATUSES:
        return
    if previous is not None and previous.get("status") == payload["status"] and int(previous.get("attempt", 1)) == int(payload.get("attempt", 1)):
        return
    history_entry = {
        "timestamp": payload["updated_at"],
        "run_id": run_id,
        "phase": payload["phase"],
        "objective_id": payload["objective_id"],
        "activity_id": payload["activity_id"],
        "kind": payload["kind"],
        "display_name": payload["display_name"],
        "status": payload["status"],
        "attempt": int(payload.get("attempt", 1)),
        "status_reason": payload.get("status_reason"),
        "recovery_action": payload.get("recovery_action"),
        "current_activity": payload.get("current_activity"),
    }
    append_jsonl(project_root / "runs" / run_id / "live" / "activity-history.jsonl", history_entry)
    app_root = find_objective_app_root(project_root, payload["objective_id"])
    if app_root is not None:
        append_jsonl(app_root / "orchestrator" / "activity-logs" / f"{run_id}.jsonl", history_entry)
