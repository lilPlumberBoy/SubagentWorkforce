from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .bundles import active_bundle_ids_for_phase
from .filesystem import ensure_dir, load_optional_json, read_json, write_json
from .live import (
    list_activities,
    mark_activity_interrupted,
    mark_activity_recovered,
    now_timestamp,
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
            if result["status"] == "blocked" and result.get("blocking", True):
                summary["blocked"].append(result)
    bundle_incidents = reconcile_bundle_landings(
        project_root,
        run_id,
        phase=run_state["current_phase"],
        apply=apply,
    )
    summary["bundle_incidents"] = bundle_incidents
    for incident in bundle_incidents:
        if incident.get("status") == "blocked":
            summary["blocked"].append(incident)
    if apply:
        refresh_run_state(project_root, run_id)
        _reconcile_autonomy_state(project_root, run_id)
    return summary


def _reconcile_autonomy_state(project_root: Path, run_id: str) -> None:
    from .autonomy import autonomy_lease_is_fresh

    run_dir = project_root / "runs" / run_id
    autonomy_path = run_dir / "autonomy.json"
    if not autonomy_path.exists():
        return
    autonomy_state = load_optional_json(autonomy_path)
    if not isinstance(autonomy_state, dict):
        return
    if autonomy_state.get("status") not in {"active", "running"}:
        return
    phase_plan = read_json(run_dir / "phase-plan.json")
    if all(item.get("status") == "complete" for item in phase_plan.get("phases", [])):
        autonomy_state["status"] = "completed"
        autonomy_state["completed_at"] = autonomy_state.get("completed_at") or now_timestamp()
        autonomy_state["stop_phase"] = None
        autonomy_state["stop_reason"] = None
        autonomy_state["lease_owner"] = None
        autonomy_state["lease_started_at"] = None
        autonomy_state["lease_heartbeat_at"] = None
        autonomy_state["lease_timeout_seconds"] = None
        autonomy_state["lease_action_kind"] = None
        autonomy_state["updated_at"] = now_timestamp()
        write_json(autonomy_path, autonomy_state)
        return
    run_state = read_run_state(project_root, run_id)
    if run_state.get("active_activity_ids") or run_state.get("queued_activity_ids"):
        return
    if autonomy_lease_is_fresh(autonomy_state):
        return
    phase = run_state.get("current_phase")
    if not phase:
        return
    activities = list_activities(project_root, run_id, phase=phase)
    phase_report = load_optional_json(run_dir / "phase-reports" / f"{phase}.json")
    if isinstance(phase_report, dict):
        recommendation = str(phase_report.get("recommendation") or "").strip()
        if recommendation == "hold":
            stop_reason = "Phase is on hold and no live work remains."
        elif recommendation == "advance":
            stop_reason = "Phase is ready to advance, but no live work remains."
        else:
            stop_reason = "Phase work is no longer running, but the autonomous controller remained active."
    elif any(activity.get("status") in {"interrupted", "recovered", "failed", "blocked", "needs_revision"} for activity in activities):
        stop_reason = "Phase work was interrupted or reconciled, and no live work remains."
    else:
        return
    autonomy_state["status"] = "stopped"
    autonomy_state["stop_phase"] = phase
    autonomy_state["stop_reason"] = stop_reason
    autonomy_state["updated_at"] = now_timestamp()
    write_json(autonomy_path, autonomy_state)


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
    incidents.extend(load_active_bundle_recovery_incidents(project_root, run_id, phase))
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
    recovered_from_artifact = False

    if process_alive(activity.get("process_metadata")):
        return None

    artifact_status = artifact_reconciliation["status"]
    if artifact_status in {"completed", "ready_for_bundle_review", "blocked", "needs_revision"}:
        if status not in {artifact_status, "recovered"}:
            if artifact_status == "completed":
                next_status = "recovered" if status in RECOVERABLE_ACTIVE_STATUSES or status == "interrupted" else "completed"
            else:
                next_status = artifact_status
                recovered_from_artifact = True
            reason = (
                "Recovered task status from validated final report on disk."
                if activity["kind"] == "task_execution" and artifact_status != "completed"
                else "Validated final artifacts on disk after interruption."
            )
            recovery_action = "validated_artifact"
    elif artifact_status == "failed":
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
        "blocking": not (activity["kind"] == "task_execution" and next_status == "blocked"),
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
        update_kwargs = {
            "status": next_status,
            "progress_stage": next_status,
            "status_reason": reason,
            "recovery_action": recovery_action,
            "artifact_reconciliation": artifact_reconciliation,
            "current_activity": reason,
        }
        if recovered_from_artifact:
            update_kwargs["recovered_at"] = now_timestamp()
        update_activity(project_root, run_id, activity_id, **update_kwargs)
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
            if (
                path.name.endswith(".summary.json")
                or path.name.endswith(".last-message.json")
                or path.name.endswith(".prompt.json")
                or path.name.endswith(".outline.json")
            ):
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
            payload = read_json(output_path)
            validate_document(payload, "completion-report.v1", project_root)
            details.append(f"valid completion report at {activity['output_path']}")
            report_status = str(payload.get("status", "completed"))
            if report_status not in {"completed", "ready_for_bundle_review", "blocked", "needs_revision"}:
                report_status = "completed"
            return {"status": report_status, "details": details}
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


def bundle_recovery_summary_path(run_dir: Path, bundle_id: str) -> Path:
    return run_dir / "recovery" / f"{bundle_id}.json"


def bundle_recovery_archive_path(run_dir: Path, phase: str, bundle_id: str) -> Path:
    archive_dir = ensure_dir(run_dir / "archive" / "bundle-recovery" / phase)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", now_timestamp())
    candidate = archive_dir / f"{bundle_id}-{stem}.json"
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = archive_dir / f"{bundle_id}-{stem}-{counter}.json"
    return candidate


def archive_bundle_recovery_incident(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    bundle_id: str,
    reason: str,
    apply: bool,
) -> dict[str, Any] | None:
    run_dir = project_root / "runs" / run_id
    summary_path = bundle_recovery_summary_path(run_dir, bundle_id)
    if not summary_path.exists():
        return None
    archive_path = bundle_recovery_archive_path(run_dir, phase, bundle_id)
    payload = load_optional_json(summary_path)
    archive_payload = payload if isinstance(payload, dict) else {"bundle_id": bundle_id, "phase": phase}
    archive_payload["status"] = "archived"
    archive_payload["archived_at"] = now_timestamp()
    archive_payload["archive_reason"] = reason
    archive_payload["archived_from"] = display_path(project_root, summary_path)
    archive_payload["bundle_id"] = bundle_id
    archive_payload["phase"] = phase
    incident = {
        "bundle_id": bundle_id,
        "status": "archived",
        "reason": reason,
        "artifact_path": display_path(project_root, archive_path),
    }
    if not apply:
        return incident
    write_json(archive_path, archive_payload)
    summary_path.unlink()
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=None,
        event_type="bundle.recovery_archived",
        message=f"Archived obsolete bundle recovery incident for {bundle_id}.",
        payload={
            "bundle_id": bundle_id,
            "reason": reason,
            "archive_path": display_path(project_root, archive_path),
        },
    )
    return incident


def load_active_bundle_recovery_incidents(project_root: Path, run_id: str, phase: str) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    incidents: list[dict[str, Any]] = []
    recovery_dir = run_dir / "recovery"
    if not recovery_dir.exists():
        return incidents
    for path in sorted(recovery_dir.glob("*.json")):
        payload = load_optional_json(path)
        if not isinstance(payload, dict):
            continue
        if payload.get("phase") != phase:
            continue
        if payload.get("status") == "archived":
            continue
        bundle_id = str(payload.get("bundle_id") or path.stem).strip()
        reason = str(payload.get("reason") or "Bundle landing recovery blocked.").strip()
        incidents.append(
            {
                "activity_id": f"bundle:{bundle_id}",
                "status": "blocked",
                "reason": reason,
                "artifact_path": display_path(project_root, path),
            }
        )
    return incidents


def reconcile_bundle_landings(project_root: Path, run_id: str, *, phase: str, apply: bool) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    incidents: list[dict[str, Any]] = []
    try:
        repo_root = git_root(project_root)
    except WorktreeError:
        return incidents
    integration_branch = integration_branch_name(run_id)
    integration_workspace = integration_workspace_path(project_root, run_id)
    active_bundle_ids = active_bundle_ids_for_phase(run_dir, phase)
    bundles_by_id: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "bundles").glob("*.json")):
        bundle = read_json(path)
        bundle_id = str(bundle.get("bundle_id") or "").strip()
        if bundle_id:
            bundles_by_id[bundle_id] = bundle

    recovery_dir = run_dir / "recovery"
    recovery_paths = sorted(recovery_dir.glob("*.json")) if recovery_dir.exists() else []
    for path in recovery_paths:
        payload = load_optional_json(path)
        if not isinstance(payload, dict):
            continue
        if payload.get("phase") != phase:
            continue
        bundle_id = str(payload.get("bundle_id") or path.stem).strip()
        if not bundle_id:
            continue
        bundle = bundles_by_id.get(bundle_id)
        if bundle_id not in active_bundle_ids:
            archive_bundle_recovery_incident(
                project_root,
                run_id,
                phase=phase,
                bundle_id=bundle_id,
                reason="Bundle is no longer part of the active manager-approved bundle plan.",
                apply=apply,
            )
            continue
        if bundle is None:
            archive_bundle_recovery_incident(
                project_root,
                run_id,
                phase=phase,
                bundle_id=bundle_id,
                reason="Bundle recovery incident no longer has a corresponding bundle file.",
                apply=apply,
            )
            continue
        if bundle.get("status") == "rejected":
            archive_bundle_recovery_incident(
                project_root,
                run_id,
                phase=phase,
                bundle_id=bundle_id,
                reason="Bundle is rejected and no longer an active landing candidate.",
                apply=apply,
            )

    for path in sorted((run_dir / "bundles").glob("*.json")):
        bundle = read_json(path)
        if bundle.get("phase") != phase:
            continue
        bundle_id = str(bundle.get("bundle_id") or "").strip()
        if not bundle_id or bundle_id not in active_bundle_ids:
            archive_bundle_recovery_incident(
                project_root,
                run_id,
                phase=phase,
                bundle_id=bundle_id,
                reason="Bundle is not part of the active manager-approved bundle plan.",
                apply=apply,
            )
            continue
        if bundle.get("status") not in {"accepted", "blocked"}:
            archive_bundle_recovery_incident(
                project_root,
                run_id,
                phase=phase,
                bundle_id=bundle_id,
                reason=f"Bundle status {bundle.get('status')} is not an active accepted landing state.",
                apply=apply,
            )
            continue
        landing_results = bundle.get("landing_results", [])
        if landing_results:
            archive_bundle_recovery_incident(
                project_root,
                run_id,
                phase=phase,
                bundle_id=bundle_id,
                reason="Bundle already has landing results and no longer needs landing recovery.",
                apply=apply,
            )
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
            archive_bundle_recovery_incident(
                project_root,
                run_id,
                phase=phase,
                bundle_id=bundle_id,
                reason="Bundle does not contain isolated-write tasks and has no landing recovery surface.",
                apply=apply,
            )
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
            archive_bundle_recovery_incident(
                project_root,
                run_id,
                phase=phase,
                bundle_id=bundle_id,
                reason="Accepted landing is already present on the integration branch.",
                apply=apply,
            )
            incidents.append(
                {
                    "bundle_id": bundle_id,
                    "status": "completed",
                    "reason": "Accepted landing already present on run integration branch.",
                }
            )
            continue
        summary_path = bundle_recovery_summary_path(run_dir, bundle_id)
        summary_payload = {
            "run_id": run_id,
            "bundle_id": bundle_id,
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
                "bundle_id": bundle_id,
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
            message=f"Bundle {bundle_id} requires recovery before landing can continue.",
            payload={"bundle_id": bundle_id, "recovery_path": display_path(project_root, summary_path)},
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
