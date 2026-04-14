from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .collaboration import resolve_collaboration_request
from .constants import PHASES
from .filesystem import ensure_dir, load_optional_json, read_json, write_json
from .live import now_timestamp, record_event, refresh_run_state
from .objective_planner import plan_objective
from .objective_roots import capability_owned_path_hints
from .planner import CAPABILITY_KEYWORDS
from .reports import generate_phase_report
from .schemas import validate_document


FEEDBACK_CAPABILITY_HINTS = {
    "frontend": ("input", "typing", "selection", "cursor", "button", "form", "render", "browser", "ui", "edit"),
    "backend": ("request", "response", "api", "server", "database", "persist", "validation", "route", "contract"),
    "middleware": ("runtime", "startup", "delivery", "integration", "script", "build", "bundle", "release"),
}
MAX_FEEDBACK_COLLABORATION_REPAIR_ATTEMPTS = 1


def feedback_dir(run_dir: Path) -> Path:
    return ensure_dir(run_dir / "feedback")


def next_feedback_id(run_dir: Path) -> str:
    directory = feedback_dir(run_dir)
    prefix = "FBK-"
    max_index = 0
    for path in directory.glob(f"{prefix}*.json"):
        suffix = path.stem.replace(prefix, "", 1)
        if suffix.isdigit():
            max_index = max(max_index, int(suffix))
    return f"{prefix}{max_index + 1:03d}"


def list_feedback(project_root: Path, run_id: str) -> list[dict[str, Any]]:
    directory = feedback_dir(project_root / "runs" / run_id)
    feedback_items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        feedback_items.append(read_json(path))
    return feedback_items


def submit_feedback(
    project_root: Path,
    run_id: str,
    *,
    summary: str,
    expected_behavior: str = "",
    observed_behavior: str = "",
    repro_steps: list[str] | None = None,
    severity: str = "medium",
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    current_phase = str(read_json(run_dir / "phase-plan.json").get("current_phase") or "discovery")
    payload = {
        "schema": "user-feedback.v1",
        "run_id": run_id,
        "feedback_id": next_feedback_id(run_dir),
        "summary": summary.strip(),
        "expected_behavior": expected_behavior.strip(),
        "observed_behavior": observed_behavior.strip(),
        "repro_steps": [step.strip() for step in (repro_steps or []) if isinstance(step, str) and step.strip()],
        "severity": severity,
        "status": "submitted",
        "triage": None,
        "resolution": None,
        "submitted_at": now_timestamp(),
        "replacement_plan_revision": None,
    }
    validate_document(payload, "user-feedback.v1", project_root)
    path = feedback_dir(run_dir) / f"{payload['feedback_id']}.json"
    write_json(path, payload)
    record_event(
        project_root,
        run_id,
        phase=current_phase,
        activity_id=None,
        event_type="feedback.submitted",
        message=f"User feedback {payload['feedback_id']} was submitted.",
        payload={
            "feedback_id": payload["feedback_id"],
            "severity": payload["severity"],
            "summary": payload["summary"],
        },
    )
    return triage_feedback(project_root, run_id, payload["feedback_id"])


def triage_feedback(project_root: Path, run_id: str, feedback_id: str) -> dict[str, Any]:
    path = feedback_dir(project_root / "runs" / run_id) / f"{feedback_id}.json"
    payload = read_json(path)
    objective_map = read_json(project_root / "runs" / run_id / "objective-map.json")
    phase_plan = read_json(project_root / "runs" / run_id / "phase-plan.json")

    scored_objectives = _score_objectives_for_feedback(objective_map.get("objectives", []), _feedback_text(payload))
    matched_objective_ids = [item["objective_id"] for item in scored_objectives if item["score"] > 0]
    triage = build_feedback_triage(project_root, run_id, payload, scored_objectives, phase_plan=phase_plan)
    payload["triage"] = triage
    payload["status"] = "approved" if triage["actionable"] else "pending_human_review"
    if payload["status"] != "resolved":
        payload["resolution"] = None
    validate_document(payload, "user-feedback.v1", project_root)
    write_json(path, payload)
    record_event(
        project_root,
        run_id,
        phase=triage.get("required_reentry_phase"),
        activity_id=None,
        event_type="feedback.triaged",
        message=f"User feedback {feedback_id} was triaged as {triage['feedback_kind']}.",
        payload={
            "feedback_id": feedback_id,
            "status": payload["status"],
            "route": triage["route"],
            "owner_objective_id": triage["owner_objective_id"],
            "owner_capability": triage["owner_capability"],
            "matched_objective_ids": matched_objective_ids,
        },
    )
    return payload


def build_feedback_triage(
    project_root: Path,
    run_id: str,
    feedback: dict[str, Any],
    scored_objectives: list[dict[str, Any]],
    *,
    phase_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    phase_plan = phase_plan or read_json(project_root / "runs" / run_id / "phase-plan.json")
    matched = [item for item in scored_objectives if item["score"] > 0]
    current_phase = str(phase_plan.get("current_phase") or "discovery")
    all_complete = all(str(item.get("status")) == "complete" for item in phase_plan.get("phases", []))
    cross_boundary_reentry_phase = "design" if current_phase in {"design", "mvp-build", "polish"} or all_complete else current_phase

    if not matched:
        return {
            "feedback_kind": "feature_request",
            "goal_alignment": "adjacent",
            "route": "manual_review",
            "owner_objective_id": None,
            "owner_capability": None,
            "required_reentry_phase": "polish" if all_complete else current_phase,
            "actionable": False,
            "rationale": "No objective or capability matched the reported behavior strongly enough for deterministic routing.",
            "matched_objective_ids": [],
            "focus_paths": [],
        }

    top = matched[0]
    second = matched[1] if len(matched) > 1 else None
    strong_matches = [item for item in matched if int(item.get("score") or 0) >= 8]
    owner_objective_id = str(top["objective_id"])
    owner_capability = str(top["capability"]) if top.get("capability") else None
    matched_objective_ids = [str(item["objective_id"]) for item in matched]
    required_reentry_phase = "polish" if all_complete or current_phase == "polish" else current_phase

    if len(strong_matches) >= 2 or (second is not None and int(top["score"]) <= int(second["score"]) + 1):
        return {
            "feedback_kind": "cross_boundary_change",
            "goal_alignment": "in_goal",
            "route": "cross_boundary_change",
            "owner_objective_id": owner_objective_id,
            "owner_capability": owner_capability,
            "required_reentry_phase": cross_boundary_reentry_phase,
            "actionable": True,
            "rationale": "Multiple objectives matched the reported behavior, so the system will route it through the shared cross-boundary change workflow.",
            "matched_objective_ids": matched_objective_ids,
            "focus_paths": _ordered_unique(
                path
                for objective_id in matched_objective_ids
                for capability in (
                    str(item.get("capability") or "").strip()
                    for item in matched
                    if str(item.get("objective_id") or "").strip() == objective_id
                )
                for path in capability_owned_path_hints(
                    project_root,
                    objective_id,
                    capability,
                    phase=cross_boundary_reentry_phase,
                )[:3]
            )[:12],
        }

    focus_paths = capability_owned_path_hints(
        project_root,
        owner_objective_id,
        owner_capability or "",
        phase=required_reentry_phase,
    )[:8]
    return {
        "feedback_kind": "local_bug",
        "goal_alignment": "in_goal",
        "route": "local_repair",
        "owner_objective_id": owner_objective_id,
        "owner_capability": owner_capability,
        "required_reentry_phase": required_reentry_phase,
        "actionable": True,
        "rationale": "The report maps cleanly to one owned objective and capability, so it can be repaired as bounded local work.",
        "matched_objective_ids": matched_objective_ids,
        "focus_paths": focus_paths,
    }


def active_approved_feedback(
    project_root: Path,
    run_id: str,
    feedback_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    refresh_feedback_resolution_state(project_root, run_id, feedback_ids=feedback_ids)
    current_phase = str(
        (read_json(project_root / "runs" / run_id / "phase-plan.json").get("current_phase") or "discovery")
    )
    current_phase_index = PHASES.index(current_phase) if current_phase in PHASES else 0
    selected = set(feedback_ids or [])
    feedback_items: list[dict[str, Any]] = []
    for payload in list_feedback(project_root, run_id):
        if selected and payload["feedback_id"] not in selected:
            continue
        if payload.get("status") not in {"approved", "in_progress"}:
            continue
        if payload.get("resolution") is not None:
            continue
        triage = payload.get("triage")
        if not isinstance(triage, dict) or not triage.get("actionable"):
            continue
        required_reentry_phase = str(triage.get("required_reentry_phase") or "").strip()
        if (
            not selected
            and required_reentry_phase in PHASES
            and PHASES.index(required_reentry_phase) > current_phase_index
        ):
            continue
        feedback_items.append(payload)
    return feedback_items


def approved_feedback_reentry_state(
    project_root: Path,
    run_id: str,
    *,
    feedback_ids: list[str] | None = None,
) -> dict[str, Any]:
    selected_feedback = active_approved_feedback(project_root, run_id, feedback_ids)
    local_feedback = [
        item
        for item in selected_feedback
        if isinstance(item.get("triage"), dict) and item["triage"].get("route") == "local_repair"
    ]
    if not local_feedback:
        return {
            "selected_feedback": selected_feedback,
            "local_feedback": [],
            "reentry_phase": None,
            "objective_ids": [],
            "blocking_activities": [],
        }

    reentry_phase = min(
        (
            str(item["triage"]["required_reentry_phase"])
            for item in local_feedback
            if item.get("triage", {}).get("required_reentry_phase")
        ),
        key=lambda phase: PHASES.index(phase),
    )
    objective_ids = _ordered_unique(
        str(item["triage"]["owner_objective_id"])
        for item in local_feedback
        if item.get("triage", {}).get("owner_objective_id")
    )
    blocking_activities: list[dict[str, Any]] = []
    if objective_ids:
        from .change_replan import active_replan_blockers

        blocking_activities = active_replan_blockers(project_root, run_id, objective_ids)
    return {
        "selected_feedback": selected_feedback,
        "local_feedback": local_feedback,
        "reentry_phase": reentry_phase,
        "objective_ids": objective_ids,
        "blocking_activities": blocking_activities,
    }


def cross_boundary_feedback_items(selected_feedback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in selected_feedback
        if isinstance(item.get("triage"), dict) and item["triage"].get("route") == "cross_boundary_change"
    ]


def feedback_change_output_ids(
    project_root: Path,
    run_id: str,
    *,
    objective_ids: list[str],
    reentry_phase: str,
) -> list[str]:
    run_dir = project_root / "runs" / run_id
    phase_order = {phase: index for index, phase in enumerate(PHASES)}
    selected_objectives = set(objective_ids)
    outputs: list[str] = []
    phase_floor = phase_order[reentry_phase]
    tasks = sorted(
        (
            payload
            for payload in _phase_tasks(run_dir, reentry_phase)
            if str(payload.get("objective_id") or "").strip() in selected_objectives
        ),
        key=lambda task: str(task.get("task_id") or ""),
    )
    if not tasks:
        for path in sorted((run_dir / "tasks").glob("*.json")):
            payload = load_optional_json(path)
            if not isinstance(payload, dict):
                continue
            if str(payload.get("objective_id") or "").strip() not in selected_objectives:
                continue
            task_phase = str(payload.get("phase") or "")
            if task_phase not in phase_order or phase_order[task_phase] < phase_floor:
                continue
            tasks.append(payload)
    for task in tasks:
        for output in task.get("expected_outputs", []):
            if not isinstance(output, dict):
                continue
            output_id = str(output.get("output_id") or "").strip()
            if output_id:
                outputs.append(output_id)
    return _ordered_unique(outputs)


def persist_cross_boundary_feedback_change_requests(
    project_root: Path,
    run_id: str,
    *,
    feedback_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not feedback_items:
        return []
    run_dir = project_root / "runs" / run_id
    persisted: list[dict[str, Any]] = []
    for feedback_item in feedback_items:
        triage = feedback_item.get("triage") or {}
        matched_objective_ids = _ordered_unique(triage.get("matched_objective_ids") or [])
        reentry_phase = str(triage.get("required_reentry_phase") or "design")
        affected_output_ids = feedback_change_output_ids(
            project_root,
            run_id,
            objective_ids=matched_objective_ids,
            reentry_phase=reentry_phase,
        )
        if not affected_output_ids:
            raise ValueError(
                f"Cross-boundary feedback {feedback_item['feedback_id']} could not determine any affected outputs."
            )
        from .changes import next_change_request_id

        synthetic_task_id = f"feedback-{feedback_item['feedback_id']}"
        change_id = next_change_request_id(run_dir, synthetic_task_id)
        payload = {
            "schema": "change-request.v2",
            "run_id": run_id,
            "change_id": change_id,
            "source_task_id": synthetic_task_id,
            "source_objective_id": str(triage.get("owner_objective_id") or matched_objective_ids[0]),
            "phase": reentry_phase,
            "change_category": "shared_behavior",
            "summary": str(feedback_item.get("summary") or "").strip(),
            "blocking_reason": (
                "User-requested cross-boundary feature change affects multiple owned objectives and shared behavior."
            ),
            "why_local_resolution_is_invalid": (
                "A local-only repair would fork shared behavior across objectives instead of changing the authoritative contract."
            ),
            "blocking": True,
            "goal_critical": True,
            "affected_output_ids": affected_output_ids,
            "affected_handoff_ids": [],
            "impacted_objective_ids": matched_objective_ids,
            "impacted_task_ids": [],
            "required_reentry_phase": reentry_phase,
            "impact": {
                "goal_changed": False,
                "scope_changed": False,
                "boundary_changed": False,
                "interface_changed": True,
                "architecture_changed": False,
                "team_changed": False,
                "implementation_changed": True,
            },
            "approval": {"mode": "human", "status": "approved"},
            "replacement_plan_revision": None,
        }
        validate_document(payload, "change-request.v2", project_root)
        write_json(run_dir / "change-requests" / f"{change_id}.json", payload)
        persisted.append(payload)
        record_event(
            project_root,
            run_id,
            phase=reentry_phase,
            activity_id=None,
            event_type="feedback.cross_boundary_change_created",
            message=f"Synthesized approved cross-boundary change request {change_id} from {feedback_item['feedback_id']}.",
            payload={
                "feedback_id": feedback_item["feedback_id"],
                "change_id": change_id,
                "affected_output_ids": affected_output_ids,
                "matched_objective_ids": matched_objective_ids,
            },
        )
    return persisted


def refresh_feedback_resolution_state(
    project_root: Path,
    run_id: str,
    *,
    feedback_ids: list[str] | None = None,
) -> list[str]:
    run_dir = project_root / "runs" / run_id
    selected = set(feedback_ids or [])
    phase_plan = read_json(run_dir / "phase-plan.json")
    changed_feedback_ids: list[str] = []
    for payload in list_feedback(project_root, run_id):
        if selected and payload["feedback_id"] not in selected:
            continue
        triage = payload.get("triage") or {}
        reentry_phase = str(triage.get("required_reentry_phase") or phase_plan.get("current_phase") or "discovery")
        phase_report = load_optional_json(run_dir / "phase-reports" / f"{reentry_phase}.json") or {}
        if payload.get("status") in {"approved", "in_progress"} and payload.get("resolution") is None:
            if (
                payload.get("replacement_plan_revision")
                and _feedback_report_satisfies_objective_constraints(
                project_root,
                run_id,
                feedback_item=payload,
                reentry_phase=reentry_phase,
                phase_report=phase_report,
                )
            ):
                payload["status"] = "resolved"
                payload["resolution"] = {
                    "status": "resolved",
                    "resolved_at": now_timestamp(),
                    "phase_report_path": f"runs/{run_id}/phase-reports/{reentry_phase}.json",
                    "validation_report_path": (
                        str((phase_report.get("release_validation_summary") or {}).get("report_path") or "") or None
                    ),
                    "notes": "Validated after applying targeted user feedback repair work.",
                }
                validate_document(payload, "user-feedback.v1", project_root)
                write_json(feedback_dir(run_dir) / f"{payload['feedback_id']}.json", payload)
                changed_feedback_ids.append(payload["feedback_id"])
                record_event(
                    project_root,
                    run_id,
                    phase=reentry_phase,
                    activity_id=None,
                    event_type="feedback.resolved",
                    message=f"Feedback {payload['feedback_id']} resolved from current validated run state.",
                    payload={"feedback_id": payload["feedback_id"]},
                )
            continue
        if payload.get("status") != "resolved" or not payload.get("resolution"):
            continue
        if _feedback_report_satisfies_objective_constraints(
            project_root,
            run_id,
            feedback_item=payload,
            reentry_phase=reentry_phase,
            phase_report=phase_report,
        ):
            continue
        payload["status"] = "approved" if triage.get("actionable") and triage.get("route") == "local_repair" else "pending_human_review"
        payload["resolution"] = None
        validate_document(payload, "user-feedback.v1", project_root)
        write_json(feedback_dir(run_dir) / f"{payload['feedback_id']}.json", payload)
        changed_feedback_ids.append(payload["feedback_id"])
        record_event(
            project_root,
            run_id,
            phase=reentry_phase,
            activity_id=None,
            event_type="feedback.reopened",
            message=f"Feedback {payload['feedback_id']} was reopened because its resolution evidence is no longer valid.",
            payload={"feedback_id": payload["feedback_id"]},
        )
    return changed_feedback_ids


def apply_feedback_and_resume(
    project_root: Path,
    run_id: str,
    *,
    feedback_ids: list[str] | None = None,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    timeout_seconds: int | None = None,
    max_concurrency: int = 3,
) -> dict[str, Any]:
    reentry_state = approved_feedback_reentry_state(project_root, run_id, feedback_ids=feedback_ids)
    selected_feedback = list(reentry_state["selected_feedback"])
    if not selected_feedback:
        return {
            "run_id": run_id,
            "applied_feedback_ids": [],
            "reentry_phase": None,
            "replanned_objective_ids": [],
            "resumed": False,
            "resume_summary": None,
        }

    local_feedback = list(reentry_state["local_feedback"])
    cross_boundary_feedback = cross_boundary_feedback_items(selected_feedback)
    if local_feedback and cross_boundary_feedback:
        raise ValueError(
            "Cannot apply mixed local-repair and cross-boundary feedback in one pass. Re-run with explicit feedback_ids."
        )
    if cross_boundary_feedback:
        change_requests = persist_cross_boundary_feedback_change_requests(
            project_root,
            run_id,
            feedback_items=cross_boundary_feedback,
        )
        if not change_requests:
            raise ValueError("Approved cross-boundary feedback did not produce any actionable change requests.")
        change_reentry_phase = min(
            (str(item["required_reentry_phase"]) for item in change_requests),
            key=lambda phase: PHASES.index(phase),
        )
        replacement_plan_revision = f"feedback-change:{change_reentry_phase}:{now_timestamp()}"
        mark_feedback_in_progress(
            project_root,
            run_id,
            [item["feedback_id"] for item in cross_boundary_feedback],
            replacement_plan_revision,
        )
        from .change_replan import apply_approved_changes_and_resume

        resume_summary = apply_approved_changes_and_resume(
            project_root,
            run_id,
            change_ids=[item["change_id"] for item in change_requests],
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
        )
        phase_report, _ = generate_phase_report(project_root, run_id)
        resolved_feedback_ids = resolve_feedback_if_validated(
            project_root,
            run_id,
            [item["feedback_id"] for item in cross_boundary_feedback],
            reentry_phase=change_reentry_phase,
            phase_report=phase_report,
        )
        record_event(
            project_root,
            run_id,
            phase=change_reentry_phase,
            activity_id=None,
            event_type="feedback.replan_completed",
            message=f"Applied approved cross-boundary user feedback to {len(change_requests)} change requests.",
            payload={
                "feedback_ids": [item["feedback_id"] for item in cross_boundary_feedback],
                "change_ids": [item["change_id"] for item in change_requests],
                "replacement_plan_revision": replacement_plan_revision,
                "resolved_feedback_ids": resolved_feedback_ids,
            },
        )
        return {
            "run_id": run_id,
            "applied_feedback_ids": [item["feedback_id"] for item in cross_boundary_feedback],
            "applied_change_ids": [item["change_id"] for item in change_requests],
            "reentry_phase": change_reentry_phase,
            "replanned_objective_ids": resume_summary.get("replanned_objective_ids", []),
            "replacement_plan_revision": replacement_plan_revision,
            "resolved_feedback_ids": resolved_feedback_ids,
            "resumed": True,
            "resume_summary": resume_summary,
        }
    if not local_feedback:
        raise ValueError("Approved feedback is not currently actionable as local repair work.")

    reentry_phase = reentry_state["reentry_phase"]
    objective_ids = list(reentry_state["objective_ids"])
    if not objective_ids:
        raise ValueError("Feedback triage did not identify any objective to replan.")

    run_dir = project_root / "runs" / run_id
    record_event(
        project_root,
        run_id,
        phase=reentry_phase,
        activity_id=None,
        event_type="feedback.replan_started",
        message=f"Applying approved user feedback requires reentry at {reentry_phase}.",
        payload={
            "feedback_ids": [item["feedback_id"] for item in local_feedback],
            "objective_ids": objective_ids,
            "reentry_phase": reentry_phase,
        },
    )

    from .change_replan import (
        archive_future_phase_objective_artifacts,
        rewind_run_for_reentry,
    )

    blocking_activities = list(reentry_state["blocking_activities"])
    if blocking_activities:
        blocker_summary = ", ".join(
            f"{activity['activity_id']} ({activity['status']})" for activity in blocking_activities
        )
        raise ValueError(
            "Cannot apply approved feedback while targeted objectives still have active work: "
            + blocker_summary
        )

    rewind_run_for_reentry(project_root, run_id, reentry_phase)
    archive_future_phase_objective_artifacts(project_root, run_id, reentry_phase, objective_ids)

    for objective_id in objective_ids:
        objective_feedback = [item for item in local_feedback if item["triage"]["owner_objective_id"] == objective_id]
        repair_context = build_feedback_repair_context(
            project_root,
            run_id,
            objective_id=objective_id,
            reentry_phase=reentry_phase,
            feedback_items=objective_feedback,
        )
        plan_objective(
            project_root,
            run_id,
            objective_id,
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            replace=True,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
            allow_recovery_blocked=True,
            refresh_phase_summary=False,
            repair_context=repair_context,
        )
        write_feedback_repair_contexts(
            project_root,
            run_id,
            objective_id=objective_id,
            reentry_phase=reentry_phase,
            repair_context=repair_context,
        )

    replacement_plan_revision = f"feedback:{reentry_phase}:{now_timestamp()}"
    mark_feedback_in_progress(project_root, run_id, [item["feedback_id"] for item in local_feedback], replacement_plan_revision)
    refresh_run_state(project_root, run_id)

    from .management import run_phase

    try:
        resume_summary = run_phase(
            project_root,
            run_id,
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
        )
        phase_report, _ = generate_phase_report(project_root, run_id)
        collaboration_repair_passes = 0
        while True:
            actionable_requests = actionable_feedback_collaboration_requests(
                project_root,
                run_id,
                objective_ids=objective_ids,
            )
            if not actionable_requests:
                break
            request_ids = [str(item["request_id"]) for item in actionable_requests]
            if collaboration_repair_attempts(
                project_root,
                run_id,
                phase=reentry_phase,
                request_ids=request_ids,
            ) >= MAX_FEEDBACK_COLLABORATION_REPAIR_ATTEMPTS:
                break
            grouped_requests: dict[str, list[dict[str, Any]]] = {}
            for request in actionable_requests:
                grouped_requests.setdefault(str(request["objective_id"]), []).append(request)
            record_event(
                project_root,
                run_id,
                phase=reentry_phase,
                activity_id=None,
                event_type="feedback.collaboration_repair_requested",
                message="Retrying feedback repair after manager collaboration requests blocked the current task scope.",
                payload={
                    "feedback_ids": [item["feedback_id"] for item in local_feedback],
                    "request_ids": request_ids,
                    "objective_ids": sorted(grouped_requests),
                },
            )
            for objective_id in sorted(grouped_requests):
                objective_feedback = [item for item in local_feedback if item["triage"]["owner_objective_id"] == objective_id]
                repair_context = build_feedback_repair_context(
                    project_root,
                    run_id,
                    objective_id=objective_id,
                    reentry_phase=reentry_phase,
                    feedback_items=objective_feedback,
                    collaboration_requests=grouped_requests[objective_id],
                )
                plan_objective(
                    project_root,
                    run_id,
                    objective_id,
                    sandbox_mode=sandbox_mode,
                    codex_path=codex_path,
                    replace=True,
                    timeout_seconds=timeout_seconds,
                    max_concurrency=max_concurrency,
                    allow_recovery_blocked=True,
                    refresh_phase_summary=False,
                    repair_context=repair_context,
                )
                write_feedback_repair_contexts(
                    project_root,
                    run_id,
                    objective_id=objective_id,
                    reentry_phase=reentry_phase,
                    repair_context=repair_context,
                )
                for request in grouped_requests[objective_id]:
                    resolve_collaboration_request(project_root, run_id, str(request["request_id"]))
            refresh_run_state(project_root, run_id)
            resume_summary = run_phase(
                project_root,
                run_id,
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                timeout_seconds=timeout_seconds,
                max_concurrency=max_concurrency,
            )
            phase_report, _ = generate_phase_report(project_root, run_id)
            collaboration_repair_passes += 1
            if collaboration_repair_passes >= MAX_FEEDBACK_COLLABORATION_REPAIR_ATTEMPTS:
                break
        resolved_feedback_ids = resolve_feedback_if_validated(
            project_root,
            run_id,
            [item["feedback_id"] for item in local_feedback],
            reentry_phase=reentry_phase,
            phase_report=phase_report,
        )
        record_event(
            project_root,
            run_id,
            phase=reentry_phase,
            activity_id=None,
            event_type="feedback.replan_completed",
            message=f"Applied approved user feedback to {len(objective_ids)} objectives.",
            payload={
                "feedback_ids": [item["feedback_id"] for item in local_feedback],
                "objective_ids": objective_ids,
                "replacement_plan_revision": replacement_plan_revision,
                "resolved_feedback_ids": resolved_feedback_ids,
            },
        )
        return {
            "run_id": run_id,
            "applied_feedback_ids": [item["feedback_id"] for item in local_feedback],
            "reentry_phase": reentry_phase,
            "replanned_objective_ids": objective_ids,
            "replacement_plan_revision": replacement_plan_revision,
            "resolved_feedback_ids": resolved_feedback_ids,
            "resumed": True,
            "resume_summary": resume_summary,
        }
    finally:
        clear_feedback_repair_contexts(
            project_root,
            run_id,
            reentry_phase=reentry_phase,
            objective_ids=objective_ids,
        )


def actionable_feedback_collaboration_requests(
    project_root: Path,
    run_id: str,
    *,
    objective_ids: list[str],
) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    collaboration_dir = run_dir / "collaboration"
    if not collaboration_dir.exists():
        return []
    allowed_objectives = set(objective_ids)
    requests: list[dict[str, Any]] = []
    for path in sorted(collaboration_dir.glob("*.json")):
        payload = read_json(path)
        if payload.get("status") != "open":
            continue
        if not payload.get("blocking", True):
            continue
        objective_id = str(payload.get("objective_id") or "").strip()
        if objective_id not in allowed_objectives:
            continue
        request_type = str(payload.get("type") or "").strip()
        to_role = str(payload.get("to_role") or "").strip()
        if request_type not in {"contract_resolution", "scope_repair"} and not to_role.endswith("-manager") and not to_role.endswith(".manager"):
            continue
        payload["_path"] = path
        requests.append(payload)
    return requests


def collaboration_repair_attempts(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    request_ids: list[str],
) -> int:
    attempts = 0
    request_id_set = set(request_ids)
    if not request_id_set:
        return attempts
    events_path = project_root / "runs" / run_id / "live" / "events.jsonl"
    if not events_path.exists():
        return attempts
    for raw_line in events_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = read_json_line(raw_line)
        except ValueError:
            continue
        if event.get("phase") != phase or event.get("event_type") != "feedback.collaboration_repair_requested":
            continue
        payload = event.get("payload") or {}
        event_request_ids = {
            str(value).strip()
            for value in payload.get("request_ids", [])
            if isinstance(value, str) and str(value).strip()
        }
        if event_request_ids & request_id_set:
            attempts += 1
    return attempts


def read_json_line(raw_line: str) -> dict[str, Any]:
    value = json.loads(raw_line)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object line.")
    return value


def extract_focus_paths_from_collaboration_summary(summary: str) -> list[str]:
    candidates = re.findall(r"`([^`]+)`", summary)
    paths: list[str] = []
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized:
            continue
        if "/" not in normalized:
            continue
        if normalized.startswith("runs/"):
            continue
        paths.append(normalized)
    return _ordered_unique(paths)


def collect_existing_feedback_scope_files(project_root: Path, focus_paths: list[str], *, limit: int = 8) -> list[str]:
    concrete_paths: list[str] = []
    for value in focus_paths:
        normalized = str(value).strip()
        if not normalized:
            continue
        if any(token in normalized for token in ("*", "?", "[")):
            for candidate in sorted(project_root.glob(normalized)):
                if not candidate.exists() or candidate.is_dir():
                    continue
                concrete_paths.append(candidate.relative_to(project_root).as_posix())
            continue
        candidate = project_root / normalized
        if not candidate.exists() or candidate.is_dir():
            continue
        concrete_paths.append(normalized)
    ranked = sorted(_ordered_unique(concrete_paths), key=_feedback_scope_file_rank)
    return ranked[:limit]


def _feedback_scope_file_rank(path: str) -> tuple[int, int, str]:
    normalized = str(path).strip()
    priority = 50
    if normalized.startswith("apps/todo/frontend/src/todos/"):
        priority = 0
    elif normalized.startswith("apps/todo/frontend/src/"):
        priority = 1
    elif normalized.startswith("apps/todo/frontend/test/"):
        priority = 2
    elif normalized.startswith("apps/todo/frontend/"):
        priority = 3
    elif normalized.startswith("apps/todo/backend/design/"):
        priority = 4
    elif normalized.startswith("runs/"):
        priority = 8
    filename = Path(normalized).name
    filename_priority = 0
    if filename == "app.js":
        filename_priority = -2
    elif filename in {"TodoApp.jsx", "TodoApp.js"}:
        filename_priority = -1
    elif filename == "index.js":
        filename_priority = 1
    elif ".test." in filename:
        filename_priority = 2
    return (priority, filename_priority, len(normalized), normalized)

    reentry_phase = min(
        (
            str(item["triage"]["required_reentry_phase"])
            for item in local_feedback
            if item.get("triage", {}).get("required_reentry_phase")
        ),
        key=lambda phase: PHASES.index(phase),
    )
    objective_ids = _ordered_unique(
        str(item["triage"]["owner_objective_id"])
        for item in local_feedback
        if item.get("triage", {}).get("owner_objective_id")
    )
    if not objective_ids:
        raise ValueError("Feedback triage did not identify any objective to replan.")

    run_dir = project_root / "runs" / run_id
    record_event(
        project_root,
        run_id,
        phase=reentry_phase,
        activity_id=None,
        event_type="feedback.replan_started",
        message=f"Applying approved user feedback requires reentry at {reentry_phase}.",
        payload={
            "feedback_ids": [item["feedback_id"] for item in local_feedback],
            "objective_ids": objective_ids,
            "reentry_phase": reentry_phase,
        },
    )

    from .change_replan import (
        active_replan_blockers,
        archive_future_phase_objective_artifacts,
        rewind_run_for_reentry,
    )

    blocking_activities = active_replan_blockers(project_root, run_id, objective_ids)
    if blocking_activities:
        blocker_summary = ", ".join(
            f"{activity['activity_id']} ({activity['status']})" for activity in blocking_activities
        )
        raise ValueError(
            "Cannot apply approved feedback while targeted objectives still have active work: "
            + blocker_summary
        )

    rewind_run_for_reentry(project_root, run_id, reentry_phase)
    archive_future_phase_objective_artifacts(project_root, run_id, reentry_phase, objective_ids)

    for objective_id in objective_ids:
        objective_feedback = [item for item in local_feedback if item["triage"]["owner_objective_id"] == objective_id]
        plan_objective(
            project_root,
            run_id,
            objective_id,
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            replace=True,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
            allow_recovery_blocked=True,
            refresh_phase_summary=False,
            repair_context=build_feedback_repair_context(
                project_root,
                run_id,
                objective_id=objective_id,
                reentry_phase=reentry_phase,
                feedback_items=objective_feedback,
            ),
        )

    replacement_plan_revision = f"feedback:{reentry_phase}:{now_timestamp()}"
    mark_feedback_in_progress(project_root, run_id, [item["feedback_id"] for item in local_feedback], replacement_plan_revision)
    refresh_run_state(project_root, run_id)

    from .management import run_phase

    resume_summary = run_phase(
        project_root,
        run_id,
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
    )
    phase_report, _ = generate_phase_report(project_root, run_id)
    collaboration_repair_passes = 0
    while True:
        actionable_requests = actionable_feedback_collaboration_requests(
            project_root,
            run_id,
            objective_ids=objective_ids,
        )
        if not actionable_requests:
            break
        request_ids = [str(item["request_id"]) for item in actionable_requests]
        if collaboration_repair_attempts(
            project_root,
            run_id,
            phase=reentry_phase,
            request_ids=request_ids,
        ) >= MAX_FEEDBACK_COLLABORATION_REPAIR_ATTEMPTS:
            break
        grouped_requests: dict[str, list[dict[str, Any]]] = {}
        for request in actionable_requests:
            grouped_requests.setdefault(str(request["objective_id"]), []).append(request)
        record_event(
            project_root,
            run_id,
            phase=reentry_phase,
            activity_id=None,
            event_type="feedback.collaboration_repair_requested",
            message="Retrying feedback repair after manager collaboration requests blocked the current task scope.",
            payload={
                "feedback_ids": [item["feedback_id"] for item in local_feedback],
                "request_ids": request_ids,
                "objective_ids": sorted(grouped_requests),
            },
        )
        for objective_id in sorted(grouped_requests):
            objective_feedback = [item for item in local_feedback if item["triage"]["owner_objective_id"] == objective_id]
            plan_objective(
                project_root,
                run_id,
                objective_id,
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                replace=True,
                timeout_seconds=timeout_seconds,
                max_concurrency=max_concurrency,
                allow_recovery_blocked=True,
                refresh_phase_summary=False,
                repair_context=build_feedback_repair_context(
                    project_root,
                    run_id,
                    objective_id=objective_id,
                    reentry_phase=reentry_phase,
                    feedback_items=objective_feedback,
                    collaboration_requests=grouped_requests[objective_id],
                ),
            )
            for request in grouped_requests[objective_id]:
                resolve_collaboration_request(project_root, run_id, str(request["request_id"]))
        refresh_run_state(project_root, run_id)
        resume_summary = run_phase(
            project_root,
            run_id,
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
        )
        phase_report, _ = generate_phase_report(project_root, run_id)
        collaboration_repair_passes += 1
        if collaboration_repair_passes >= MAX_FEEDBACK_COLLABORATION_REPAIR_ATTEMPTS:
            break
    resolved_feedback_ids = resolve_feedback_if_validated(
        project_root,
        run_id,
        [item["feedback_id"] for item in local_feedback],
        reentry_phase=reentry_phase,
        phase_report=phase_report,
    )
    record_event(
        project_root,
        run_id,
        phase=reentry_phase,
        activity_id=None,
        event_type="feedback.replan_completed",
        message=f"Applied approved user feedback to {len(objective_ids)} objectives.",
        payload={
            "feedback_ids": [item["feedback_id"] for item in local_feedback],
            "objective_ids": objective_ids,
            "replacement_plan_revision": replacement_plan_revision,
            "resolved_feedback_ids": resolved_feedback_ids,
        },
    )
    return {
        "run_id": run_id,
        "applied_feedback_ids": [item["feedback_id"] for item in local_feedback],
        "reentry_phase": reentry_phase,
        "replanned_objective_ids": objective_ids,
        "replacement_plan_revision": replacement_plan_revision,
        "resolved_feedback_ids": resolved_feedback_ids,
        "resumed": True,
        "resume_summary": resume_summary,
    }


def build_feedback_repair_context(
    project_root: Path,
    run_id: str,
    *,
    objective_id: str,
    reentry_phase: str,
    feedback_items: list[dict[str, Any]],
    collaboration_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    focus_paths: list[str] = []
    included_task_ids = [
        task["task_id"]
        for task in _phase_tasks(project_root / "runs" / run_id, reentry_phase)
        if task["objective_id"] == objective_id
    ]
    user_feedback: list[dict[str, Any]] = []
    rejection_reasons: list[str] = []
    collaboration_items: list[dict[str, Any]] = []
    owner_capability = None
    for item in feedback_items:
        triage = item.get("triage") or {}
        owner_capability = owner_capability or triage.get("owner_capability")
        for path in triage.get("focus_paths", []):
            if isinstance(path, str) and path.strip():
                focus_paths.append(path.strip())
        feedback_details = {
            "feedback_id": item["feedback_id"],
            "summary": item["summary"],
            "expected_behavior": item.get("expected_behavior", ""),
            "observed_behavior": item.get("observed_behavior", ""),
            "repro_steps": list(item.get("repro_steps", [])),
        }
        user_feedback.append(feedback_details)
        rejection_reasons.append(
            f"{item['feedback_id']}: {item['summary']} | expected: {item.get('expected_behavior', '').strip() or 'n/a'} "
            f"| observed: {item.get('observed_behavior', '').strip() or 'n/a'}"
        )
    for request in collaboration_requests or []:
        summary = str(request.get("summary") or "").strip()
        request_id = str(request.get("request_id") or "").strip()
        request_type = str(request.get("type") or "").strip()
        focus_paths.extend(extract_focus_paths_from_collaboration_summary(summary))
        collaboration_items.append(
            {
                "request_id": request_id,
                "type": request_type,
                "summary": summary,
                "to_role": str(request.get("to_role") or "").strip(),
            }
        )
        reason_prefix = f"{request_id}: " if request_id else ""
        rejection_reasons.append(f"{reason_prefix}{summary}")
    existing_file_hints = collect_existing_feedback_scope_files(project_root, focus_paths)
    return {
        "source": "user_feedback",
        "compact_prompt": True,
        "reason": "Repair the owned objective so the approved user feedback is resolved without broadening scope.",
        "focus_paths": _ordered_unique(focus_paths),
        "existing_file_hints": existing_file_hints,
        "included_task_ids": included_task_ids,
        "rejection_reasons": rejection_reasons,
        "user_feedback": user_feedback,
        "collaboration_requests": collaboration_items,
        "owner_capability": owner_capability,
    }


def feedback_repair_context_dir(project_root: Path, run_id: str) -> Path:
    return ensure_dir(project_root / "runs" / run_id / "repair-contexts")


def write_feedback_repair_contexts(
    project_root: Path,
    run_id: str,
    *,
    objective_id: str,
    reentry_phase: str,
    repair_context: dict[str, Any],
) -> None:
    tasks = [
        task
        for task in _phase_tasks(project_root / "runs" / run_id, reentry_phase)
        if task.get("objective_id") == objective_id
    ]
    task_ids = [str(task.get("task_id") or "").strip() for task in tasks if str(task.get("task_id") or "").strip()]
    clear_feedback_repair_contexts(
        project_root,
        run_id,
        reentry_phase=reentry_phase,
        objective_ids=[objective_id],
    )
    if not task_ids:
        return
    context_dir = feedback_repair_context_dir(project_root, run_id)
    for task_id in task_ids:
        payload = dict(repair_context)
        payload["task_id"] = task_id
        payload["included_task_ids"] = task_ids
        write_json(context_dir / f"{task_id}.json", payload)


def clear_feedback_repair_contexts(
    project_root: Path,
    run_id: str,
    *,
    reentry_phase: str,
    objective_ids: list[str],
) -> None:
    context_dir = project_root / "runs" / run_id / "repair-contexts"
    if not context_dir.exists():
        return
    task_ids = {
        str(task.get("task_id") or "").strip()
        for task in _phase_tasks(project_root / "runs" / run_id, reentry_phase)
        if task.get("objective_id") in set(objective_ids)
    }
    for task_id in task_ids:
        path = context_dir / f"{task_id}.json"
        if path.exists():
            path.unlink()


def mark_feedback_in_progress(
    project_root: Path,
    run_id: str,
    feedback_ids: list[str],
    replacement_plan_revision: str,
) -> list[dict[str, Any]]:
    selected = set(feedback_ids)
    updated: list[dict[str, Any]] = []
    for payload in list_feedback(project_root, run_id):
        if payload["feedback_id"] not in selected:
            continue
        payload["status"] = "in_progress"
        payload["replacement_plan_revision"] = replacement_plan_revision
        validate_document(payload, "user-feedback.v1", project_root)
        write_json(feedback_dir(project_root / "runs" / run_id) / f"{payload['feedback_id']}.json", payload)
        updated.append(payload)
    return updated


def resolve_feedback_if_validated(
    project_root: Path,
    run_id: str,
    feedback_ids: list[str],
    *,
    reentry_phase: str,
    phase_report: dict[str, Any],
) -> list[str]:
    validation_report_path = None
    if reentry_phase == "polish":
        validation_report_path = str((phase_report.get("release_validation_summary") or {}).get("report_path") or "") or None
    phase_report_path = f"runs/{run_id}/phase-reports/{reentry_phase}.json"
    resolved_ids: list[str] = []
    selected = set(feedback_ids)
    for payload in list_feedback(project_root, run_id):
        if payload["feedback_id"] not in selected:
            continue
        if not _feedback_report_satisfies_objective_constraints(
            project_root,
            run_id,
            feedback_item=payload,
            reentry_phase=reentry_phase,
            phase_report=phase_report,
        ):
            continue
        payload["status"] = "resolved"
        payload["resolution"] = {
            "status": "resolved",
            "resolved_at": now_timestamp(),
            "phase_report_path": phase_report_path,
            "validation_report_path": validation_report_path,
            "notes": "Validated after applying targeted user feedback repair work.",
        }
        validate_document(payload, "user-feedback.v1", project_root)
        write_json(feedback_dir(project_root / "runs" / run_id) / f"{payload['feedback_id']}.json", payload)
        resolved_ids.append(payload["feedback_id"])
    return resolved_ids


def _phase_report_satisfies_feedback(phase_report: dict[str, Any], *, reentry_phase: str) -> bool:
    if not isinstance(phase_report, dict):
        return False
    if reentry_phase == "polish":
        release_validation = phase_report.get("release_validation_summary")
        return isinstance(release_validation, dict) and release_validation.get("status") == "passed"
    return phase_report.get("recommendation") == "advance"


def _feedback_report_satisfies_objective_constraints(
    project_root: Path,
    run_id: str,
    *,
    feedback_item: dict[str, Any],
    reentry_phase: str,
    phase_report: dict[str, Any],
) -> bool:
    if not _phase_report_satisfies_feedback(phase_report, reentry_phase=reentry_phase):
        return False
    triage = feedback_item.get("triage") or {}
    objective_id = str(triage.get("owner_objective_id") or "").strip()
    if not objective_id:
        return False
    run_dir = project_root / "runs" / run_id
    reports_dir = run_dir / "reports"
    matched_reports = 0
    passed_validations = 0
    for path in sorted(reports_dir.glob("*.json")):
        payload = load_optional_json(path)
        if not isinstance(payload, dict):
            continue
        if payload.get("phase") != reentry_phase or payload.get("objective_id") != objective_id:
            continue
        matched_reports += 1
        if payload.get("status") in {"blocked", "failed"}:
            return False
        for result in payload.get("validation_results", []):
            if not isinstance(result, dict):
                continue
            if str(result.get("status") or "").strip() == "passed":
                passed_validations += 1
    if matched_reports == 0 or passed_validations == 0:
        return False
    collaboration_dir = run_dir / "collaboration"
    if collaboration_dir.exists():
        for path in sorted(collaboration_dir.glob("*.json")):
            payload = load_optional_json(path)
            if not isinstance(payload, dict):
                continue
            if payload.get("objective_id") != objective_id or payload.get("status") != "open":
                continue
            request_type = str(payload.get("type") or "").strip()
            to_role = str(payload.get("to_role") or "").strip()
            if payload.get("blocking", True) and request_type in {"scope_repair", "contract_resolution"}:
                return False
    change_dir = run_dir / "change-requests"
    return True


def _score_objectives_for_feedback(objectives: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    feedback_tokens = _tokenize(text)
    feedback_stems = {_stem(token) for token in feedback_tokens if _stem(token)}
    scored: list[dict[str, Any]] = []
    for objective in objectives:
        capability = _primary_capability(objective)
        score = 0
        for keyword in CAPABILITY_KEYWORDS.get(capability, ()):
            if keyword in text:
                score += 4
        for keyword in FEEDBACK_CAPABILITY_HINTS.get(capability, ()):
            if keyword in text:
                score += 3
        objective_tokens = _tokenize(f"{objective.get('title', '')} {objective.get('summary', '')}")
        objective_stems = {_stem(token) for token in objective_tokens if _stem(token)}
        score += len(objective_stems & feedback_stems) * 2
        scored.append(
            {
                "objective_id": str(objective.get("objective_id") or "").strip(),
                "capability": capability,
                "score": score,
            }
        )
    return sorted(scored, key=lambda item: (-int(item["score"]), item["objective_id"]))


def _feedback_text(feedback: dict[str, Any]) -> str:
    parts = [
        str(feedback.get("summary") or ""),
        str(feedback.get("expected_behavior") or ""),
        str(feedback.get("observed_behavior") or ""),
        " ".join(str(value) for value in feedback.get("repro_steps", []) if isinstance(value, str)),
    ]
    return " ".join(part.strip().lower() for part in parts if part and part.strip())


def _tokenize(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _stem(token: str) -> str:
    return token[:5] if len(token) >= 4 else ""


def _primary_capability(objective: dict[str, Any]) -> str:
    capabilities = objective.get("capabilities") or []
    if isinstance(capabilities, list) and capabilities:
        return str(capabilities[0])
    return ""


def _ordered_unique(values: Any) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _phase_tasks(run_dir: Path, phase: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        payload = load_optional_json(path)
        if not isinstance(payload, dict):
            continue
        if payload.get("phase") == phase:
            tasks.append(payload)
    return tasks
