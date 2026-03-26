from __future__ import annotations

from pathlib import Path
from typing import Any

from .changes import (
    active_approved_change_requests,
    earliest_required_reentry_phase,
    mark_change_requests_replanned,
)
from .constants import PHASES
from .filesystem import read_json, write_json
from .impact import ACTIVE_ACTIVITY_STATUSES, apply_approved_change_impacts
from .live import list_activities, now_timestamp, record_event, refresh_run_state
from .management import run_phase
from .objective_planner import plan_objective, quarantined_objective_phase_artifacts, write_phase_plan_summary


def apply_approved_changes_and_resume(
    project_root: Path,
    run_id: str,
    *,
    change_ids: list[str] | None = None,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    timeout_seconds: int | None = None,
    max_concurrency: int = 3,
) -> dict[str, Any]:
    requested_change_ids = list(change_ids or [])
    active_requests = active_approved_change_requests(project_root, run_id, requested_change_ids or None)
    if not active_requests:
        return {
            "run_id": run_id,
            "applied_change_ids": [],
            "reentry_phase": None,
            "replanned_objective_ids": [],
            "resumed": False,
            "resume_summary": None,
        }

    run_dir = project_root / "runs" / run_id
    phase_plan_path = run_dir / "phase-plan.json"
    phase_plan = read_json(phase_plan_path)
    active_phase = str(phase_plan["current_phase"])
    impacts = apply_approved_change_impacts(
        project_root,
        run_id,
        [request["change_id"] for request in active_requests],
    )
    active_requests = active_approved_change_requests(project_root, run_id, requested_change_ids or None)
    reentry_phase = earliest_required_reentry_phase(active_requests)
    if reentry_phase is None:
        return {
            "run_id": run_id,
            "applied_change_ids": [],
            "reentry_phase": None,
            "replanned_objective_ids": [],
            "resumed": False,
            "resume_summary": None,
        }
    if PHASES.index(reentry_phase) > PHASES.index(active_phase):
        raise ValueError(
            f"Approved changes require reentry phase {reentry_phase}, which is later than the active phase {active_phase}."
        )

    producer_objective_ids = _ordered_unique(
        producer_objective_id
        for impact in impacts
        for producer_objective_id in (
            str(item.get("producer_objective_id", "")).strip() for item in impact.get("source_revisions", [])
        )
        if producer_objective_id
    )
    if not producer_objective_ids:
        producer_objective_ids = _ordered_unique(str(request["source_objective_id"]) for request in active_requests)
    impacted_objective_ids = _ordered_unique(
        objective_id
        for request in active_requests
        for objective_id in (str(item).strip() for item in request.get("impacted_objective_ids", []))
        if objective_id
    )
    replanned_objective_ids = producer_objective_ids + [
        objective_id for objective_id in impacted_objective_ids if objective_id not in set(producer_objective_ids)
    ]
    if not replanned_objective_ids:
        raise ValueError("Approved changes did not identify any producer or impacted objectives to replan.")

    blocking_activities = _active_replan_blockers(project_root, run_id, replanned_objective_ids)
    if blocking_activities:
        blocker_summary = ", ".join(
            f"{activity['activity_id']} ({activity['status']})" for activity in blocking_activities
        )
        raise ValueError(
            "Cannot apply approved changes while targeted objectives still have active work: "
            + blocker_summary
        )

    record_event(
        project_root,
        run_id,
        phase=active_phase,
        activity_id=None,
        event_type="change.replan_started",
        message=f"Applying approved changes requires reentry from {active_phase} to {reentry_phase}.",
        payload={
            "change_ids": [request["change_id"] for request in active_requests],
            "reentry_phase": reentry_phase,
            "producer_objective_ids": producer_objective_ids,
            "impacted_objective_ids": impacted_objective_ids,
        },
    )

    _rewind_phase_plan_for_reentry(project_root, run_id, reentry_phase)
    _archive_future_phase_objective_artifacts(project_root, run_id, reentry_phase, replanned_objective_ids)
    for objective_id in replanned_objective_ids:
        plan_objective(
            project_root,
            run_id,
            objective_id,
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            replace=True,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
            refresh_phase_summary=False,
        )
    write_phase_plan_summary(project_root, run_id, reentry_phase, max_concurrency=max_concurrency)
    replacement_plan_revision = f"{reentry_phase}:{now_timestamp()}"
    updated_requests = mark_change_requests_replanned(
        project_root,
        run_id,
        [request["change_id"] for request in active_requests],
        replacement_plan_revision=replacement_plan_revision,
    )
    refresh_run_state(project_root, run_id)
    resume_summary = run_phase(
        project_root,
        run_id,
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
    )
    record_event(
        project_root,
        run_id,
        phase=reentry_phase,
        activity_id=None,
        event_type="change.replan_completed",
        message=f"Applied approved changes and replanned {len(replanned_objective_ids)} objectives.",
        payload={
            "change_ids": [request["change_id"] for request in updated_requests],
            "replacement_plan_revision": replacement_plan_revision,
            "replanned_objective_ids": replanned_objective_ids,
        },
    )
    return {
        "run_id": run_id,
        "applied_change_ids": [request["change_id"] for request in updated_requests],
        "reentry_phase": reentry_phase,
        "replanned_objective_ids": replanned_objective_ids,
        "replacement_plan_revision": replacement_plan_revision,
        "resumed": True,
        "resume_summary": resume_summary,
    }


def _ordered_unique(values: Any) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _active_replan_blockers(project_root: Path, run_id: str, objective_ids: list[str]) -> list[dict[str, Any]]:
    objective_id_set = set(objective_ids)
    blockers: list[dict[str, Any]] = []
    for activity in list_activities(project_root, run_id):
        if activity.get("objective_id") not in objective_id_set:
            continue
        if activity.get("status") not in ACTIVE_ACTIVITY_STATUSES:
            continue
        blockers.append(activity)
    return blockers


def _rewind_phase_plan_for_reentry(project_root: Path, run_id: str, reentry_phase: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase_plan_path = run_dir / "phase-plan.json"
    phase_plan = read_json(phase_plan_path)
    reentry_index = PHASES.index(reentry_phase)
    phase_plan["current_phase"] = reentry_phase
    for index, item in enumerate(phase_plan["phases"]):
        item["human_approved"] = item.get("human_approved", False) if index < reentry_index else False
        if index < reentry_index:
            item["status"] = "complete"
        elif index == reentry_index:
            item["status"] = "active"
        else:
            item["status"] = "locked"
    for phase in PHASES[reentry_index:]:
        for suffix in (".json", ".md"):
            path = run_dir / "phase-reports" / f"{phase}{suffix}"
            if path.exists():
                path.unlink()
        manager_summary_path = run_dir / "manager-runs" / f"phase-{phase}.json"
        if manager_summary_path.exists():
            manager_summary_path.unlink()
        phase_summary_path = run_dir / "manager-plans" / f"{phase}-phase-plan-summary.json"
        if phase_summary_path.exists():
            phase_summary_path.unlink()
    write_json(phase_plan_path, phase_plan)
    refresh_run_state(project_root, run_id)
    return phase_plan


def _archive_future_phase_objective_artifacts(
    project_root: Path,
    run_id: str,
    reentry_phase: str,
    objective_ids: list[str],
) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    reentry_index = PHASES.index(reentry_phase)
    archived: list[dict[str, Any]] = []
    for phase in PHASES[reentry_index + 1 :]:
        for objective_id in objective_ids:
            with quarantined_objective_phase_artifacts(
                project_root,
                run_id,
                phase,
                objective_id,
                enabled=True,
            ) as archived_info:
                if archived_info.get("archive_path") is not None or archived_info.get("archived_task_ids"):
                    archived.append(
                        {
                            "phase": phase,
                            "objective_id": objective_id,
                            "archive_path": archived_info.get("archive_path"),
                            "archived_task_ids": list(archived_info.get("archived_task_ids", [])),
                        }
                    )
    return archived
