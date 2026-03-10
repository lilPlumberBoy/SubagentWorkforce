from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .filesystem import ensure_dir, load_optional_json, read_json, write_json
from .live import (
    list_activities,
    mark_activity_interrupted,
    mark_activity_recovered,
    process_alive,
    read_run_state,
    record_event,
    refresh_run_state,
    update_activity,
)
from .schemas import SchemaValidationError, validate_document
from .worktree_manager import (
    WorktreeError,
    branch_exists,
    git,
    git_root,
    integration_branch_name,
    integration_workspace_path,
    task_branch_name,
)


RECOVERABLE_ACTIVE_STATUSES = {"prompt_rendered", "launching", "running", "finalizing", "recovering"}


class RecoveryBlockedError(RuntimeError):
    """Raised when reconciliation finds unsafe state that requires human intervention."""


def reconcile_run(project_root: Path, run_id: str, *, apply: bool = False) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    run_state = read_run_state(project_root, run_id)
    summary = {
        "run_id": run_id,
        "current_phase": run_state["current_phase"],
        "apply": apply,
        "activities": [],
        "bundle_incidents": [],
        "blocked": [],
    }
    for activity in list_activities(project_root, run_id):
        result = reconcile_activity(project_root, run_id, activity, apply=apply)
        if result is not None:
            summary["activities"].append(result)
            if result["status"] == "blocked":
                summary["blocked"].append(result)
    bundle_incidents = reconcile_bundle_landings(
        project_root,
        run_id,
        phase=run_state["current_phase"],
        apply=apply,
    )
    summary["bundle_incidents"] = bundle_incidents
    if bundle_incidents:
        summary["blocked"].extend(bundle_incidents)
    if apply:
        refresh_run_state(project_root, run_id)
    return summary


def reconcile_for_command(
    project_root: Path,
    run_id: str,
    *,
    apply: bool = True,
    allow_blocked: bool = False,
) -> dict[str, Any]:
    summary = reconcile_run(project_root, run_id, apply=apply)
    if summary["blocked"] and not allow_blocked:
        reasons = []
        for incident in summary["blocked"]:
            identifier = incident.get("bundle_id") or incident.get("activity_id") or "unknown"
            reason = incident.get("reason") or incident.get("status") or "blocked recovery incident"
            artifact = incident.get("artifact_path")
            if artifact:
                reasons.append(f"{identifier}: {reason} ({artifact})")
            else:
                reasons.append(f"{identifier}: {reason}")
        joined = "; ".join(reasons)
        raise RecoveryBlockedError(
            f"Run {run_id} has blocked recovery incidents that require manual intervention: {joined}"
        )
    return summary


def summarize_recovery_for_phase(project_root: Path, run_id: str, phase: str) -> dict[str, Any]:
    activities = list_activities(project_root, run_id, phase=phase)
    events = [
        event
        for event in (load_optional_event_lines(project_root, run_id))
        if event.get("phase") == phase
    ]
    interrupted = [activity for activity in activities if activity["status"] == "interrupted"]
    recovered = [activity for activity in activities if activity["status"] in {"recovered"} or activity.get("recovered_at")]
    abandoned_events = [event for event in events if event.get("event_type") == "activity.retry_started"]
    incidents = []
    for activity in interrupted + recovered:
        incidents.append(
            {
                "activity_id": activity["activity_id"],
                "status": activity["status"],
                "reason": activity.get("status_reason") or activity.get("current_activity") or "reconciled activity",
            }
        )
    for event in events:
        if event.get("event_type") != "bundle.recovery_blocked":
            continue
        payload = event.get("payload", {})
        incidents.append(
            {
                "activity_id": f"bundle:{payload.get('bundle_id', 'unknown')}",
                "status": "blocked",
                "reason": event.get("message") or "Bundle landing recovery blocked.",
                "artifact_path": payload.get("recovery_path"),
            }
        )
    return {
        "interrupted_activities": len(interrupted),
        "recovered_activities": len(recovered),
        "abandoned_attempts": len(abandoned_events),
        "incidents": incidents,
    }


def reconcile_activity(
    project_root: Path,
    run_id: str,
    activity: dict[str, Any],
    *,
    apply: bool,
) -> dict[str, Any] | None:
    activity_id = activity["activity_id"]
    status = activity["status"]
    reason = None
    next_status = None
    recovery_action = None
    artifact_reconciliation = inspect_activity_artifacts(project_root, run_id, activity)

    if process_alive(activity.get("process_metadata")):
        return None

    if artifact_reconciliation["status"] == "completed":
        if status not in {"completed", "recovered"}:
            next_status = "recovered" if status in RECOVERABLE_ACTIVE_STATUSES or status == "interrupted" else "completed"
            reason = "Validated final artifacts on disk after interruption."
            recovery_action = "validated_artifact"
    elif artifact_reconciliation["status"] == "failed":
        if status != "failed":
            next_status = "failed"
            reason = "Recovered terminal failure from persisted execution artifacts."
            recovery_action = "validated_failure_artifact"
    elif status in RECOVERABLE_ACTIVE_STATUSES:
        next_status = "interrupted"
        if artifact_reconciliation["details"]:
            reason = "Process missing; partial artifacts found."
        else:
            reason = "Process missing with no valid final artifact."
        recovery_action = "await_retry"
    elif status == "interrupted":
        return None

    if next_status is None:
        return None

    payload = {
        "activity_id": activity_id,
        "kind": activity["kind"],
        "status": next_status,
        "reason": reason,
        "artifact_reconciliation": artifact_reconciliation,
    }
    if not apply:
        return payload

    if next_status == "interrupted":
        mark_activity_interrupted(
            project_root,
            run_id,
            activity_id,
            reason=reason,
            artifact_reconciliation=artifact_reconciliation,
            recovery_action=recovery_action,
        )
        record_event(
            project_root,
            run_id,
            phase=activity["phase"],
            activity_id=activity_id,
            event_type="activity.reconciled.interrupted",
            message=f"Reconciled {activity_id} as interrupted.",
            payload={"reason": reason, "attempt": activity.get("attempt", 1)},
        )
    elif next_status == "recovered":
        mark_activity_recovered(
            project_root,
            run_id,
            activity_id,
            reason=reason,
            recovery_action=recovery_action or "validated_artifact",
            artifact_reconciliation=artifact_reconciliation,
        )
        record_event(
            project_root,
            run_id,
            phase=activity["phase"],
            activity_id=activity_id,
            event_type="activity.recovered",
            message=f"Reconciled {activity_id} from artifacts on disk.",
            payload={"reason": reason, "attempt": activity.get("attempt", 1)},
        )
    else:
        update_activity(
            project_root,
            run_id,
            activity_id,
            status=next_status,
            progress_stage=next_status,
            status_reason=reason,
            recovery_action=recovery_action,
            artifact_reconciliation=artifact_reconciliation,
            current_activity=reason,
        )
        record_event(
            project_root,
            run_id,
            phase=activity["phase"],
            activity_id=activity_id,
            event_type=f"activity.reconciled.{next_status}",
            message=f"Reconciled {activity_id} as {next_status}.",
            payload={"reason": reason, "attempt": activity.get("attempt", 1)},
        )
    return payload


def inspect_activity_artifacts(project_root: Path, run_id: str, activity: dict[str, Any]) -> dict[str, Any]:
    if activity["kind"] == "task_execution":
        return inspect_task_artifacts(project_root, run_id, activity)
    return inspect_planning_artifacts(project_root, run_id, activity)


def inspect_planning_artifacts(project_root: Path, run_id: str, activity: dict[str, Any]) -> dict[str, Any]:
    output_path = resolve_artifact_path(project_root, activity.get("output_path"))
    details: list[str] = []
    schema_name = "objective-plan.v1" if activity["kind"] == "objective_plan" else "capability-plan.v1"
    if output_path and output_path.exists():
        try:
            validate_document(read_json(output_path), schema_name, project_root)
            details.append(f"valid {schema_name} artifact at {activity['output_path']}")
            return {"status": "completed", "details": details}
        except SchemaValidationError:
            details.append(f"invalid {schema_name} artifact at {activity['output_path']}")
    if output_path and output_path.suffix == ".json":
        outline_path = output_path.with_suffix(".outline.json")
        if outline_path.exists():
            try:
                validate_document(read_json(outline_path), "objective-outline.v1", project_root)
                details.append(f"valid objective outline at {display_path(project_root, outline_path)}")
            except SchemaValidationError:
                details.append(f"invalid objective outline at {display_path(project_root, outline_path)}")
        prefix = output_path.stem
        plans_dir = output_path.parent
        for path in sorted(plans_dir.glob(f"{prefix}-*.json")):
            if path.name.endswith(".summary.json") or path.name.endswith(".last-message.json"):
                continue
            try:
                validate_document(read_json(path), "capability-plan.v1", project_root)
                details.append(f"valid capability plan at {display_path(project_root, path)}")
            except SchemaValidationError:
                details.append(f"invalid capability plan at {display_path(project_root, path)}")
    for key in ("stdout_path", "stderr_path"):
        path = resolve_artifact_path(project_root, activity.get(key))
        if path and path.exists():
            details.append(f"partial artifact at {activity[key]}")
    return {"status": "partial" if details else "missing", "details": details}


def inspect_task_artifacts(project_root: Path, run_id: str, activity: dict[str, Any]) -> dict[str, Any]:
    output_path = resolve_artifact_path(project_root, activity.get("output_path"))
    details: list[str] = []
    if output_path and output_path.exists():
        try:
            validate_document(read_json(output_path), "completion-report.v1", project_root)
            details.append(f"valid completion report at {activity['output_path']}")
            return {"status": "completed", "details": details}
        except SchemaValidationError:
            details.append(f"invalid completion report at {activity['output_path']}")
    summary_path = execution_summary_path(project_root, activity)
    if summary_path and summary_path.exists():
        payload = read_json(summary_path)
        if payload.get("status") == "failed":
            details.append(f"failed execution summary at {display_path(project_root, summary_path)}")
            return {"status": "failed", "details": details}
        details.append(f"partial execution summary at {display_path(project_root, summary_path)}")
    for key in ("stdout_path", "stderr_path"):
        path = resolve_artifact_path(project_root, activity.get(key))
        if path and path.exists():
            details.append(f"partial artifact at {activity[key]}")
    if activity.get("workspace_path"):
        workspace_path = Path(str(activity["workspace_path"]))
        if not workspace_path.is_absolute():
            workspace_path = (project_root / workspace_path).resolve()
        if workspace_path.exists():
            details.append(f"workspace exists at {display_path(project_root, workspace_path)}")
    return {"status": "partial" if details else "missing", "details": details}


def execution_summary_path(project_root: Path, activity: dict[str, Any]) -> Path | None:
    stdout_path = resolve_artifact_path(project_root, activity.get("stdout_path"))
    if stdout_path is None or stdout_path.suffix != ".jsonl":
        return None
    return stdout_path.with_suffix("").with_suffix(".json")


def resolve_artifact_path(project_root: Path, path_value: str | None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = (project_root / path).resolve()
    return path


def display_path(project_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return str(path)


def reconcile_bundle_landings(project_root: Path, run_id: str, *, phase: str, apply: bool) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    incidents: list[dict[str, Any]] = []
    try:
        repo_root = git_root(project_root)
    except WorktreeError:
        return incidents
    integration_branch = integration_branch_name(run_id)
    integration_workspace = integration_workspace_path(project_root, run_id)
    for path in sorted((run_dir / "bundles").glob("*.json")):
        bundle = read_json(path)
        if bundle.get("phase") != phase:
            continue
        landing_results = bundle.get("landing_results", [])
        if landing_results:
            continue
        isolated_task_ids = []
        for task_id in bundle.get("included_tasks", []):
            task_path = run_dir / "tasks" / f"{task_id}.json"
            if not task_path.exists():
                continue
            task = read_json(task_path)
            if task.get("execution_mode") == "isolated_write":
                isolated_task_ids.append(task_id)
        if not isolated_task_ids:
            continue
        merged = True
        for task_id in isolated_task_ids:
            branch_name = task_branch_name(run_id, task_id)
            if not branch_exists(repo_root, branch_name):
                merged = False
                break
            check = git(repo_root, ["merge-base", "--is-ancestor", branch_name, integration_branch], check=False)
            if check.returncode != 0:
                merged = False
                break
        if merged:
            incidents.append(
                {
                    "bundle_id": bundle["bundle_id"],
                    "status": "completed",
                    "reason": "Accepted landing already present on run integration branch.",
                }
            )
            continue
        summary_path = ensure_dir(run_dir / "recovery") / f"{bundle['bundle_id']}.json"
        summary_payload = {
            "run_id": run_id,
            "bundle_id": bundle["bundle_id"],
            "phase": bundle["phase"],
            "objective_id": bundle["objective_id"],
            "status": "interrupted",
            "reason": "Accepted bundle has isolated-write tasks without completed landing results.",
            "integration_branch": integration_branch,
            "integration_workspace": display_path(project_root, integration_workspace),
            "task_ids": isolated_task_ids,
        }
        incidents.append(
            {
                "bundle_id": bundle["bundle_id"],
                "status": "blocked",
                "reason": summary_payload["reason"],
                "artifact_path": display_path(project_root, summary_path),
            }
        )
        if not apply:
            continue
        write_json(summary_path, summary_payload)
        bundle["status"] = "blocked"
        bundle["rejection_reasons"] = [summary_payload["reason"]]
        write_json(path, bundle)
        record_event(
            project_root,
            run_id,
            phase=bundle["phase"],
            activity_id=None,
            event_type="bundle.recovery_blocked",
            message=f"Bundle {bundle['bundle_id']} requires recovery before landing can continue.",
            payload={"bundle_id": bundle["bundle_id"], "recovery_path": display_path(project_root, summary_path)},
        )
    return incidents


def prepare_activity_retry(
    project_root: Path,
    run_id: str,
    activity_id: str,
    *,
    reason: str,
) -> dict[str, Any] | None:
    activity = load_optional_json((project_root / "runs" / run_id / "live" / "activities" / f"{activity_id.replace(':', '__')}.json"))
    if activity is None:
        return None
    prior_attempt = int(activity.get("attempt", 1))
    if activity.get("status") in {"interrupted", "failed", "recovered"}:
        update_activity(
            project_root,
            run_id,
            activity_id,
            status="abandoned",
            progress_stage="abandoned",
            status_reason=reason,
            superseded_by=f"{activity_id}@attempt-{prior_attempt + 1}",
            current_activity=reason,
        )
        record_event(
            project_root,
            run_id,
            phase=activity["phase"],
            activity_id=activity_id,
            event_type="activity.retry_started",
            message=f"Retrying {activity_id} after interruption.",
            payload={"prior_attempt": prior_attempt, "reason": reason},
        )
    return activity


def load_optional_event_lines(project_root: Path, run_id: str) -> list[dict[str, Any]]:
    events_path = project_root / "runs" / run_id / "live" / "events.jsonl"
    if not events_path.exists():
        return []
    events = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        events.append(json.loads(stripped))
    return events
