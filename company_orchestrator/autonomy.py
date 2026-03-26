from __future__ import annotations

from pathlib import Path
from typing import Any

from .change_replan import apply_approved_changes_and_resume
from .filesystem import append_jsonl, load_optional_json, write_json
from .live import now_timestamp, record_event
from .management import run_guidance, run_phase
from .observability import recommend_runtime_tuning
from .objective_planner import plan_phase
from .reports import advance_phase, record_human_approval
from .schemas import validate_document

DEFAULT_STOP_CONDITIONS = [
    "blocked_recovery",
    "timeout_exhausted",
    "merge_conflict",
    "blocked_handoff",
    "active_external_work",
]


def autonomy_history_path(project_root: Path, run_id: str) -> Path:
    return project_root / "runs" / run_id / "live" / "autonomy-history.jsonl"


def autonomy_state_path(project_root: Path, run_id: str) -> Path:
    return project_root / "runs" / run_id / "autonomy.json"


def default_autonomy_state(run_id: str) -> dict[str, Any]:
    return {
        "schema": "autonomy-state.v1",
        "run_id": run_id,
        "enabled": False,
        "status": "inactive",
        "auto_approve": True,
        "sandbox_mode": "read-only",
        "max_concurrency": 3,
        "timeout_seconds": None,
        "stop_conditions": list(DEFAULT_STOP_CONDITIONS),
        "approval_scope": "all",
        "stop_before_phases": [],
        "stop_on_recovery": False,
        "adaptive_tuning": True,
        "active_phase": None,
        "started_at": None,
        "updated_at": now_timestamp(),
        "completed_at": None,
        "last_action": None,
        "last_action_status": None,
        "stop_reason": None,
        "stop_phase": None,
        "last_tuning_decision": None,
    }


def initialize_autonomy_state(project_root: Path, run_id: str) -> dict[str, Any]:
    path = autonomy_state_path(project_root, run_id)
    payload = load_optional_json(path)
    if payload is not None:
        normalized = default_autonomy_state(run_id)
        normalized.update(payload)
        validate_document(normalized, "autonomy-state.v1", project_root)
        if normalized != payload:
            write_json(path, normalized)
        return normalized
    payload = default_autonomy_state(run_id)
    validate_document(payload, "autonomy-state.v1", project_root)
    write_json(path, payload)
    return payload


def read_autonomy_state(project_root: Path, run_id: str) -> dict[str, Any]:
    return initialize_autonomy_state(project_root, run_id)


def update_autonomy_state(project_root: Path, run_id: str, **updates: Any) -> dict[str, Any]:
    payload = read_autonomy_state(project_root, run_id)
    payload.update(updates)
    payload["updated_at"] = now_timestamp()
    validate_document(payload, "autonomy-state.v1", project_root)
    write_json(autonomy_state_path(project_root, run_id), payload)
    return payload


def all_phases_complete(phase_plan: dict[str, Any]) -> bool:
    return all(item["status"] == "complete" for item in phase_plan["phases"])


def policy_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "approval_scope": state["approval_scope"],
        "stop_before_phases": list(state["stop_before_phases"]),
        "stop_on_recovery": bool(state["stop_on_recovery"]),
        "adaptive_tuning": bool(state["adaptive_tuning"]),
    }


def record_autonomy_audit(
    project_root: Path,
    run_id: str,
    *,
    phase: str | None,
    event_type: str,
    action: str | None,
    status: str,
    reason: str | None,
    state: dict[str, Any],
    guidance: dict[str, Any] | None = None,
    tuning: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": "autonomy-audit.v1",
        "run_id": run_id,
        "timestamp": now_timestamp(),
        "phase": phase,
        "event_type": event_type,
        "action": action,
        "status": status,
        "reason": reason,
        "policy_snapshot": policy_snapshot(state),
        "guidance_snapshot": None
        if guidance is None
        else {
            "run_status": guidance.get("run_status"),
            "phase_recommendation": guidance.get("phase_recommendation"),
            "next_action_command": guidance.get("next_action_command"),
        },
        "tuning_snapshot": tuning,
    }
    validate_document(payload, "autonomy-audit.v1", project_root)
    append_jsonl(autonomy_history_path(project_root, run_id), payload)
    return payload


def approval_scope_allows_phase(approval_scope: str, phase: str) -> bool:
    if approval_scope == "all":
        return True
    if approval_scope == "planning-only":
        return phase in {"discovery", "design"}
    return False


def classify_autonomy_stop(
    project_root: Path,
    run_id: str,
    guidance: dict[str, Any],
    *,
    phase: str,
    state: dict[str, Any],
) -> tuple[str, str] | None:
    if phase in set(state.get("stop_before_phases", [])):
        return "policy_stop_phase", f"Autonomy policy is configured to stop before phase {phase}."
    run_dir = project_root / "runs" / run_id
    phase_report_path = run_dir / "phase-reports" / f"{phase}.json"
    phase_report = load_optional_json(phase_report_path)
    if phase_report is not None:
        collaboration = phase_report.get("collaboration_summary", {})
        if int(collaboration.get("blocked_handoffs", 0)) > 0 and guidance.get("next_action_command") is None:
            return "blocked_handoff", "Blocked collaboration handoffs require human review."
        if (
            any(item.get("status") == "blocked" for item in phase_report.get("objective_outcomes", []))
            and guidance.get("next_action_command") is None
        ):
            return "merge_conflict", "A blocked objective or bundle requires human review before continuing."
        if state.get("stop_on_recovery"):
            recovery = phase_report.get("recovery_summary", {})
            if int(recovery.get("interrupted_activities", 0)) > 0 or int(recovery.get("recovered_activities", 0)) > 0:
                return "recovery_policy", "Autonomy policy stops after recovery activity is detected in the phase."
    activities_dir = run_dir / "live" / "activities"
    if activities_dir.exists():
        for path in sorted(activities_dir.glob("*.json")):
            activity = load_optional_json(path)
            if not activity or activity.get("phase") != phase:
                continue
            if state.get("stop_on_recovery") and activity.get("status") in {"interrupted", "recovered"}:
                return "recovery_policy", "Autonomy policy stops when interrupted or recovered activities are present."
            if activity.get("status_reason") == "timeout_exhausted":
                return "timeout_exhausted", f"Activity {activity['activity_id']} exhausted its timeout retries."
    if guidance["run_status"] == "recoverable" and not guidance.get("next_action_command"):
        return "blocked_recovery", "The run is recoverable in principle but has no safe automated next action."
    if guidance["run_status"] == "working" and guidance.get("next_action_command") is None:
        return "active_external_work", "The run is already active elsewhere; autonomy will not attach to existing live work."
    return None


def run_autonomous(
    project_root: Path,
    run_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    timeout_seconds: int | None = None,
    max_concurrency: int = 3,
    auto_approve: bool = True,
    max_iterations: int = 40,
    approval_scope: str = "all",
    stop_before_phases: list[str] | None = None,
    stop_on_recovery: bool = False,
    adaptive_tuning: bool = True,
) -> dict[str, Any]:
    phase_plan_path = project_root / "runs" / run_id / "phase-plan.json"
    phase_plan = load_optional_json(phase_plan_path)
    if phase_plan is None:
        raise ValueError(f"Run {run_id} does not exist")
    existing_state = read_autonomy_state(project_root, run_id)
    initial_guidance = run_guidance(project_root, run_id)
    if initial_guidance["run_status"] == "working" and initial_guidance.get("next_action_command") is None:
        reason = "The run is already active elsewhere; autonomy will not attach to existing live work."
        record_event(
            project_root,
            run_id,
            phase=phase_plan["current_phase"],
            activity_id=None,
            event_type="autonomy.attach_refused",
            message=f"Autonomous mode refused to attach to run {run_id}: {reason}",
            payload={"stop_condition": "active_external_work"},
        )
        record_autonomy_audit(
            project_root,
            run_id,
            phase=phase_plan["current_phase"],
            event_type="autonomy.attach_refused",
            action="run-autonomous",
            status="ignored",
            reason=reason,
            state=existing_state,
            guidance=initial_guidance,
            tuning=existing_state.get("last_tuning_decision"),
        )
        return {
            "run_id": run_id,
            "status": "stopped",
            "stop_condition": "active_external_work",
            "stop_reason": reason,
            "active_phase": phase_plan["current_phase"],
            "guidance": initial_guidance,
            "actions": [],
            "autonomy": existing_state,
        }
    state = update_autonomy_state(
        project_root,
        run_id,
        enabled=True,
        status="active",
        auto_approve=auto_approve,
        sandbox_mode=sandbox_mode,
        max_concurrency=max_concurrency,
        timeout_seconds=timeout_seconds,
        active_phase=phase_plan["current_phase"],
        approval_scope=approval_scope,
        stop_before_phases=sorted(set(stop_before_phases or [])),
        stop_on_recovery=stop_on_recovery,
        adaptive_tuning=adaptive_tuning,
        started_at=now_timestamp(),
        completed_at=None,
        last_action=None,
        last_action_status=None,
        stop_reason=None,
        stop_phase=None,
        last_tuning_decision=None,
    )
    record_event(
        project_root,
        run_id,
        phase=phase_plan["current_phase"],
        activity_id=None,
        event_type="autonomy.started",
        message=f"Autonomous mode started for run {run_id}.",
        payload={
            "auto_approve": auto_approve,
            "approval_scope": approval_scope,
            "stop_before_phases": sorted(set(stop_before_phases or [])),
            "stop_on_recovery": stop_on_recovery,
            "adaptive_tuning": adaptive_tuning,
            "sandbox_mode": sandbox_mode,
            "max_concurrency": max_concurrency,
            "timeout_seconds": timeout_seconds,
        },
    )
    record_autonomy_audit(
        project_root,
        run_id,
        phase=phase_plan["current_phase"],
        event_type="autonomy.started",
        action="run-autonomous",
        status="active",
        reason="Autonomous mode started.",
        state=state,
        guidance=None,
        tuning=None,
    )
    actions: list[dict[str, Any]] = []
    for _ in range(max_iterations):
        phase_plan = load_optional_json(phase_plan_path) or phase_plan
        active_phase = phase_plan["current_phase"]
        state = read_autonomy_state(project_root, run_id)
        if all_phases_complete(phase_plan):
            final_state = update_autonomy_state(
                project_root,
                run_id,
                status="completed",
                active_phase=active_phase,
                completed_at=now_timestamp(),
                last_action_status="completed",
            )
            record_event(
                project_root,
                run_id,
                phase=active_phase,
                activity_id=None,
                event_type="autonomy.completed",
                message=f"Autonomous mode completed run {run_id}.",
                payload={},
            )
            record_autonomy_audit(
                project_root,
                run_id,
                phase=active_phase,
                event_type="autonomy.completed",
                action=None,
                status="completed",
                reason="All phases are complete.",
                state=final_state,
                guidance=None,
                tuning=None,
            )
            return {
                "run_id": run_id,
                "status": "completed",
                "active_phase": active_phase,
                "actions": actions,
                "autonomy": final_state,
            }

        guidance = run_guidance(project_root, run_id)
        stop = classify_autonomy_stop(project_root, run_id, guidance, phase=active_phase, state=state)
        if stop is not None:
            stop_condition, reason = stop
            stopped_state = update_autonomy_state(
                project_root,
                run_id,
                status="stopped",
                active_phase=active_phase,
                stop_reason=reason,
                stop_phase=active_phase,
                last_action_status="stopped",
            )
            record_event(
                project_root,
                run_id,
                phase=active_phase,
                activity_id=None,
                event_type="autonomy.stopped",
                message=f"Autonomous mode stopped for run {run_id}: {reason}",
                payload={"stop_condition": stop_condition},
            )
            record_autonomy_audit(
                project_root,
                run_id,
                phase=active_phase,
                event_type="autonomy.stopped",
                action=None,
                status="stopped",
                reason=reason,
                state=stopped_state,
                guidance=guidance,
                tuning=stopped_state.get("last_tuning_decision"),
            )
            return {
                "run_id": run_id,
                "status": "stopped",
                "stop_condition": stop_condition,
                "stop_reason": reason,
                "active_phase": active_phase,
                "guidance": guidance,
                "actions": actions,
                "autonomy": stopped_state,
            }

        if guidance["run_status"] == "ready_for_review":
            action_name = f"approve-phase {active_phase}"
            if guidance.get("phase_recommendation") != "advance":
                stopped_state = update_autonomy_state(
                    project_root,
                    run_id,
                    status="stopped",
                    active_phase=active_phase,
                    stop_reason="Phase report is on hold and no automated recovery path is available.",
                    stop_phase=active_phase,
                    last_action=action_name,
                    last_action_status="stopped",
                )
                record_autonomy_audit(
                    project_root,
                    run_id,
                    phase=active_phase,
                    event_type="autonomy.stopped",
                    action=action_name,
                    status="stopped",
                    reason=stopped_state["stop_reason"],
                    state=stopped_state,
                    guidance=guidance,
                    tuning=stopped_state.get("last_tuning_decision"),
                )
                return {
                    "run_id": run_id,
                    "status": "stopped",
                    "stop_condition": "blocked_recovery",
                    "stop_reason": stopped_state["stop_reason"],
                    "active_phase": active_phase,
                    "guidance": guidance,
                    "actions": actions,
                    "autonomy": stopped_state,
                }
            if not auto_approve or not approval_scope_allows_phase(state["approval_scope"], active_phase):
                stopped_state = update_autonomy_state(
                    project_root,
                    run_id,
                    status="stopped",
                    active_phase=active_phase,
                    stop_reason=(
                        "Autonomy policy does not allow automatic approval for this phase."
                        if auto_approve
                        else "Autonomous mode requires auto_approve to move past review gates."
                    ),
                    stop_phase=active_phase,
                    last_action=action_name,
                    last_action_status="stopped",
                )
                record_autonomy_audit(
                    project_root,
                    run_id,
                    phase=active_phase,
                    event_type="autonomy.stopped",
                    action=action_name,
                    status="stopped",
                    reason=stopped_state["stop_reason"],
                    state=stopped_state,
                    guidance=guidance,
                    tuning=stopped_state.get("last_tuning_decision"),
                )
                return {
                    "run_id": run_id,
                    "status": "stopped",
                    "stop_condition": "review_gate_policy" if auto_approve else "review_gate",
                    "stop_reason": stopped_state["stop_reason"],
                    "active_phase": active_phase,
                    "guidance": guidance,
                    "actions": actions,
                    "autonomy": stopped_state,
                }
            record_human_approval(project_root, run_id, active_phase, True)
            update_autonomy_state(
                project_root,
                run_id,
                active_phase=active_phase,
                last_action=action_name,
                last_action_status="completed",
                stop_reason=None,
                stop_phase=None,
            )
            record_event(
                project_root,
                run_id,
                phase=active_phase,
                activity_id=None,
                event_type="autonomy.auto_approved_phase",
                message=f"Autonomous mode auto-approved phase {active_phase}.",
                payload={"phase": active_phase},
            )
            record_autonomy_audit(
                project_root,
                run_id,
                phase=active_phase,
                event_type="autonomy.auto_approved_phase",
                action=action_name,
                status="completed",
                reason=f"Phase {active_phase} was auto-approved.",
                state=read_autonomy_state(project_root, run_id),
                guidance=guidance,
                tuning=read_autonomy_state(project_root, run_id).get("last_tuning_decision"),
            )
            actions.append({"action": action_name, "phase": active_phase, "status": "completed"})
            continue

        if guidance["run_status"] == "ready_to_advance":
            previous_phase = active_phase
            updated_phase_plan = advance_phase(project_root, run_id)
            next_phase = updated_phase_plan["current_phase"]
            update_autonomy_state(
                project_root,
                run_id,
                active_phase=next_phase,
                last_action=f"advance-phase {previous_phase}",
                last_action_status="completed",
            )
            record_event(
                project_root,
                run_id,
                phase=previous_phase,
                activity_id=None,
                event_type="autonomy.advanced_phase",
                message=f"Autonomous mode advanced from {previous_phase} to {next_phase}.",
                payload={"phase": previous_phase, "next_phase": next_phase},
            )
            record_autonomy_audit(
                project_root,
                run_id,
                phase=previous_phase,
                event_type="autonomy.advanced_phase",
                action="advance-phase",
                status="completed",
                reason=f"Advanced from {previous_phase} to {next_phase}.",
                state=read_autonomy_state(project_root, run_id),
                guidance=guidance,
                tuning=read_autonomy_state(project_root, run_id).get("last_tuning_decision"),
            )
            actions.append(
                {
                    "action": "advance-phase",
                    "phase": previous_phase,
                    "next_phase": next_phase,
                    "status": "completed",
                }
            )
            continue

        next_action = guidance.get("next_action_command")
        if guidance["run_status"] in {"working", "recoverable"} and next_action:
            action_kind = "planning" if ("plan-phase" in next_action or "apply-approved-changes" in next_action) else "execution"
            tuning = recommend_runtime_tuning(
                project_root,
                run_id,
                phase=active_phase,
                action_kind=action_kind,
                requested_max_concurrency=max_concurrency,
            ) if state.get("adaptive_tuning") else {
                "action_kind": action_kind,
                "requested_max_concurrency": max_concurrency,
                "effective_max_concurrency": max_concurrency,
                "reason": "Adaptive tuning is disabled.",
                "observed_calls": 0,
                "timed_out_calls": 0,
                "retry_scheduled_calls": 0,
                "average_latency_ms": 0,
            }
            update_autonomy_state(
                project_root,
                run_id,
                last_tuning_decision=tuning,
            )
            if "plan-phase" in next_action:
                replace = "--replace" in next_action
                result = plan_phase(
                    project_root,
                    run_id,
                    sandbox_mode=sandbox_mode,
                    codex_path=codex_path,
                    replace=replace,
                    timeout_seconds=timeout_seconds,
                    max_concurrency=int(tuning["effective_max_concurrency"]),
                )
                action_name = "plan-phase --replace" if replace else "plan-phase"
            elif "apply-approved-changes" in next_action:
                result = apply_approved_changes_and_resume(
                    project_root,
                    run_id,
                    sandbox_mode=sandbox_mode,
                    codex_path=codex_path,
                    timeout_seconds=timeout_seconds,
                    max_concurrency=int(tuning["effective_max_concurrency"]),
                )
                action_name = "apply-approved-changes"
            elif "resume-phase" in next_action or "run-phase" in next_action:
                result = run_phase(
                    project_root,
                    run_id,
                    sandbox_mode=sandbox_mode,
                    codex_path=codex_path,
                    timeout_seconds=timeout_seconds,
                    max_concurrency=int(tuning["effective_max_concurrency"]),
                )
                action_name = "resume-phase" if "resume-phase" in next_action else "run-phase"
            elif "approve-phase" in next_action:
                record_human_approval(project_root, run_id, active_phase, True)
                result = read_autonomy_state(project_root, run_id)
                action_name = f"approve-phase {active_phase}"
            elif "advance-phase" in next_action:
                result = advance_phase(project_root, run_id)
                action_name = "advance-phase"
            else:
                stopped_state = update_autonomy_state(
                    project_root,
                    run_id,
                    status="stopped",
                    active_phase=active_phase,
                    stop_reason=f"Unsupported autonomous next action: {next_action}",
                    stop_phase=active_phase,
                    last_action_status="stopped",
                )
                return {
                    "run_id": run_id,
                    "status": "stopped",
                    "stop_condition": "unsupported_action",
                    "stop_reason": stopped_state["stop_reason"],
                    "active_phase": active_phase,
                    "actions": actions,
                    "autonomy": stopped_state,
                }
            update_autonomy_state(
                project_root,
                run_id,
                active_phase=(load_optional_json(phase_plan_path) or phase_plan)["current_phase"],
                last_action=action_name,
                last_action_status="completed",
                stop_reason=None,
                stop_phase=None,
            )
            record_event(
                project_root,
                run_id,
                phase=active_phase,
                activity_id=None,
                event_type="autonomy.action_completed",
                message=f"Autonomous mode completed {action_name}.",
                payload={"action": action_name},
            )
            record_autonomy_audit(
                project_root,
                run_id,
                phase=active_phase,
                event_type="autonomy.action_completed",
                action=action_name,
                status="completed",
                reason=tuning["reason"],
                state=read_autonomy_state(project_root, run_id),
                guidance=guidance,
                tuning=tuning,
            )
            actions.append({"action": action_name, "phase": active_phase, "status": "completed", "result": result})
            continue

        stopped_state = update_autonomy_state(
            project_root,
            run_id,
            status="stopped",
            active_phase=active_phase,
            stop_reason="No autonomous action was available for the current run state.",
            stop_phase=active_phase,
            last_action_status="stopped",
        )
        record_event(
            project_root,
            run_id,
            phase=active_phase,
            activity_id=None,
            event_type="autonomy.stopped",
            message=f"Autonomous mode stopped for run {run_id}: no available action.",
            payload={"run_status": guidance["run_status"]},
        )
        record_autonomy_audit(
            project_root,
            run_id,
            phase=active_phase,
            event_type="autonomy.stopped",
            action=None,
            status="stopped",
            reason=stopped_state["stop_reason"],
            state=stopped_state,
            guidance=guidance,
            tuning=stopped_state.get("last_tuning_decision"),
        )
        return {
            "run_id": run_id,
            "status": "stopped",
            "stop_condition": "no_action",
            "stop_reason": stopped_state["stop_reason"],
            "active_phase": active_phase,
            "guidance": guidance,
            "actions": actions,
            "autonomy": stopped_state,
        }

    stopped_state = update_autonomy_state(
        project_root,
        run_id,
        status="stopped",
        active_phase=(load_optional_json(phase_plan_path) or phase_plan)["current_phase"],
        stop_reason=f"Autonomous mode reached the iteration safety limit ({max_iterations}).",
        stop_phase=(load_optional_json(phase_plan_path) or phase_plan)["current_phase"],
        last_action_status="stopped",
    )
    record_event(
        project_root,
        run_id,
        phase=stopped_state["active_phase"],
        activity_id=None,
        event_type="autonomy.stopped",
        message=f"Autonomous mode stopped for run {run_id}: iteration safety limit reached.",
        payload={"max_iterations": max_iterations},
    )
    record_autonomy_audit(
        project_root,
        run_id,
        phase=stopped_state["active_phase"],
        event_type="autonomy.stopped",
        action=None,
        status="stopped",
        reason=stopped_state["stop_reason"],
        state=stopped_state,
        guidance=None,
        tuning=stopped_state.get("last_tuning_decision"),
    )
    return {
        "run_id": run_id,
        "status": "stopped",
        "stop_condition": "iteration_limit",
        "stop_reason": stopped_state["stop_reason"],
        "active_phase": stopped_state["active_phase"],
        "actions": actions,
        "autonomy": stopped_state,
    }
