from __future__ import annotations

import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from .bundle_plans import objective_bundle_specs, objective_plan_has_no_phase_work
from .bundles import assemble_review_bundle, land_accepted_bundle, review_bundle
from .changes import active_approved_change_requests
from .executor import ExecutorError, TaskExecutionRuntime, execute_task
from .feedback import active_approved_feedback, approved_feedback_reentry_state
from .filesystem import ensure_dir, load_optional_json, read_json, write_json
from .handoffs import HANDOFF_BLOCKED, blocking_handoffs_for_task, list_handoffs, refresh_handoffs_for_phase
from .impact import stale_task_notifications
from .live import activity_path, ensure_activity, initialize_live_run, list_activities, record_event, update_activity
from .objective_planner import plan_objective
from .parallelism import classify_parallel_safety, parallel_requested, warning
from .recovery import load_optional_event_lines, reconcile_for_command
from .reports import generate_phase_report
from .task_graph import active_phase_tasks, active_task_ids_by_objective_for_phase


MAX_OBJECTIVE_BUNDLE_REPAIR_ATTEMPTS = 1
MAX_POLISH_RELEASE_REPAIR_ATTEMPTS = 1
TASK_LOCAL_REPAIR_BLOCKER_KINDS = {
    "validation_command_scope",
    "validation_harness",
    "workspace_materialization",
    "local_missing_file",
    "test_flake",
}
RUN_STATE_REPAIR_BLOCKER_KINDS = {
    "ownership_boundary",
}
STRUCTURAL_REPAIR_BLOCKER_KINDS = {
    "missing_input",
    "upstream_dependency",
    "missing_handoff",
    "contract_conflict",
    "repo_layout_mismatch",
    "ownership_conflict",
    "plan_shape_mismatch",
}


def polish_hold_is_exhausted(project_root: Path, run_id: str, phase_report: dict[str, Any] | None) -> bool:
    if not isinstance(phase_report, dict):
        return False
    if phase_report.get("phase") != "polish":
        return False
    if phase_report.get("recommendation") != "hold":
        return False
    for event in load_optional_event_lines(project_root, run_id):
        if event.get("phase") != "polish":
            continue
        if event.get("event_type") == "phase.release_repair_exhausted":
            return True
    return False


def hold_recovery_requires_explicit_external_input(next_action_command: str | None) -> bool:
    if not isinstance(next_action_command, str) or not next_action_command.strip():
        return False
    return "apply-feedback" in next_action_command or "apply-approved-changes" in next_action_command


def run_phase(
    project_root: Path,
    run_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    force: bool = False,
    timeout_seconds: int | None = None,
    max_concurrency: int = 3,
) -> dict[str, Any]:
    reconcile_for_command(project_root, run_id, apply=True)
    run_dir = project_root / "runs" / run_id
    phase = active_phase(run_dir)
    ensure_phase_task_graph_integrity(project_root, run_id, phase)
    tasks = phase_tasks(run_dir, phase)
    existing_phase_report = load_optional_json(run_dir / "phase-reports" / f"{phase}.json")
    if phase == "polish" and polish_hold_is_exhausted(project_root, run_id, existing_phase_report):
        summary = {
            "run_id": run_id,
            "phase": phase,
            "scheduled": {
                "phase": phase,
                "executed": [],
                "skipped_dependency": {},
                "unresolved_dependencies": {},
                "blocked_handoffs": {},
                "failures": [],
            },
            "objectives": {},
            "phase_report_path": str((run_dir / "phase-reports" / f"{phase}.json").relative_to(project_root)),
            "recommendation": existing_phase_report["recommendation"],
            "recommended_next_command": None,
            "run_status_reason": "Polish is already on hold and the repair budget is exhausted.",
        }
        write_manager_summary(run_dir, f"phase-{phase}", summary)
        return summary
    objective_map = read_json(run_dir / "objective-map.json")
    planned_objective_ids = {
        objective["objective_id"]
        for objective in objective_map["objectives"]
        if (run_dir / "manager-plans" / f"{phase}-{objective['objective_id']}.json").exists()
    }
    objective_ids = sorted({task["objective_id"] for task in tasks} | planned_objective_ids)
    scheduler_summary = schedule_tasks(
        project_root,
        run_id,
        tasks,
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        force=force,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
    )
    objective_summaries = {}
    for objective_id in objective_ids:
        objective_summaries[objective_id] = finalize_objective_bundle(
            project_root,
            run_id,
            phase,
            objective_id,
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
            scheduler_summary=scheduler_summary,
        )
    phase_report, phase_report_path = generate_phase_report(project_root, run_id)
    release_repair_summary = None
    if phase == "polish":
        release_repair_summary = attempt_polish_release_repair(
            project_root,
            run_id,
            phase=phase,
            phase_report=phase_report,
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
        )
        if release_repair_summary is not None:
            post_repair_report = release_repair_summary.get("post_repair_report")
            if isinstance(post_repair_report, dict):
                phase_report = post_repair_report
                phase_report_path = run_dir / "phase-reports" / f"{phase}.json"
            else:
                phase_report, phase_report_path = generate_phase_report(project_root, run_id)
    summary = {
        "run_id": run_id,
        "phase": phase,
        "scheduled": scheduler_summary,
        "objectives": objective_summaries,
        "phase_report_path": str(phase_report_path.relative_to(project_root)),
        "recommendation": phase_report["recommendation"],
        "recommended_next_command": suggested_recovery_command(project_root, run_id, phase, tasks, scheduler_summary),
    }
    if release_repair_summary is not None:
        summary["release_repair"] = release_repair_summary
    write_manager_summary(run_dir, f"phase-{phase}", summary)
    return summary


def run_objective(
    project_root: Path,
    run_id: str,
    objective_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    force: bool = False,
    timeout_seconds: int | None = None,
    max_concurrency: int = 3,
) -> dict[str, Any]:
    reconcile_for_command(project_root, run_id, apply=True)
    run_dir = project_root / "runs" / run_id
    phase = active_phase(run_dir)
    ensure_phase_task_graph_integrity(project_root, run_id, phase)
    tasks = [task for task in phase_tasks(run_dir, phase) if task["objective_id"] == objective_id]
    if not tasks:
        raise ValueError(f"No tasks found for objective {objective_id} in phase {phase}")
    scheduler_summary = schedule_tasks(
        project_root,
        run_id,
        tasks,
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        force=force,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
    )
    objective_summary = finalize_objective_bundle(
        project_root,
        run_id,
        phase,
        objective_id,
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
        scheduler_summary=scheduler_summary,
    )
    summary = {
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "scheduled": scheduler_summary,
        "objective": objective_summary,
        "recommended_next_command": suggested_recovery_command(project_root, run_id, phase, tasks, scheduler_summary),
    }
    write_manager_summary(run_dir, f"{phase}-{objective_id}", summary)
    return summary


def active_phase(run_dir: Path) -> str:
    phase_plan = read_json(run_dir / "phase-plan.json")
    return phase_plan["current_phase"]


def phase_tasks(run_dir: Path, phase: str) -> list[dict[str, Any]]:
    return active_phase_tasks(run_dir, phase)


def ensure_phase_task_graph_integrity(project_root: Path, run_id: str, phase: str) -> None:
    run_dir = project_root / "runs" / run_id
    active_task_ids = active_task_ids_by_objective_for_phase(run_dir, phase)
    if not active_task_ids:
        return
    inconsistencies: list[str] = []
    terminal_statuses = {
        "abandoned",
        "interrupted",
        "recovered",
        "completed",
        "failed",
        "blocked",
        "ready_for_bundle_review",
        "accepted",
        "rejected",
        "skipped_existing",
    }
    for activity in list_activities(project_root, run_id, phase=phase):
        if activity.get("kind") != "task_execution":
            continue
        objective_id = str(activity.get("objective_id") or "").strip()
        activity_id = str(activity.get("activity_id") or "").strip()
        if not objective_id or not activity_id or objective_id not in active_task_ids:
            continue
        if activity_id in active_task_ids[objective_id]:
            continue
        status = str(activity.get("status") or "").strip() or "unknown"
        if status in terminal_statuses:
            continue
        inconsistencies.append(f"{activity_id} ({status})")
    if inconsistencies:
        raise ExecutorError(
            f"{phase} task graph is inconsistent with the active objective manifests. "
            "Refusing to schedule more work while orphan live task activity exists: "
            + ", ".join(sorted(inconsistencies))
        )


def bundle_repair_attempts(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    objective_id: str,
    bundle_id: str,
) -> int:
    attempts = 0
    for event in load_optional_event_lines(project_root, run_id):
        if event.get("phase") != phase or event.get("event_type") != "bundle.repair_requested":
            continue
        payload = event.get("payload", {})
        if payload.get("objective_id") != objective_id or payload.get("bundle_id") != bundle_id:
            continue
        attempts += 1
    return attempts


def build_bundle_repair_context(
    *,
    phase: str,
    objective_id: str,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source": "bundle_review",
        "reason": f"Repair the {phase} objective plan for {objective_id} so the rejected bundle can pass acceptance review.",
        "bundle_id": bundle.get("bundle_id"),
        "included_task_ids": list(bundle.get("included_tasks", [])),
        "rejection_reasons": list(bundle.get("rejection_reasons", [])),
    }


def build_bundle_task_repair_context(
    *,
    bundle: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    focus_paths: list[str] = []
    for blocker in report.get("blockers", []):
        if not isinstance(blocker, dict):
            continue
        paths = [
            str(value).strip()
            for value in blocker.get("related_paths", [])
            if isinstance(value, str) and str(value).strip()
        ]
        focus_paths.extend(paths)
        failures.append(
            {
                "kind": str(blocker.get("kind") or "").strip(),
                "source_test": str(blocker.get("kind") or "blocker").strip(),
                "summary": str(blocker.get("summary") or "").strip(),
                "details": str(blocker.get("details") or "").strip(),
                "paths": paths,
            }
        )
    for result in report.get("validation_results", []):
        if not isinstance(result, dict) or result.get("status") == "passed":
            continue
        failures.append(
            {
                "validation_id": str(result.get("id") or "validation").strip(),
                "source_test": str(result.get("id") or "validation").strip(),
                "excerpt": str(result.get("evidence") or "").strip(),
                "paths": [],
            }
        )
    for artifact in report.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        path = str(artifact.get("path") or "").strip()
        if path:
            focus_paths.append(path)
    return {
        "source": "bundle_task_repair",
        "summary": "Repair only the exact local task issue needed for this bundle to pass acceptance review.",
        "bundle_id": str(bundle.get("bundle_id") or "").strip(),
        "task_id": str(report.get("task_id") or "").strip(),
        "objective_id": str(report.get("objective_id") or "").strip(),
        "failures": failures,
        "focus_paths": sorted({path for path in focus_paths if path}),
    }


def build_bundle_broad_retry_context(
    *,
    bundle: dict[str, Any],
    report: dict[str, Any],
    task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    focus_paths: list[str] = []
    for blocker in report.get("blockers", []):
        if not isinstance(blocker, dict):
            continue
        paths = [
            str(value).strip()
            for value in blocker.get("related_paths", [])
            if isinstance(value, str) and str(value).strip()
        ]
        focus_paths.extend(paths)
        failures.append(
            {
                "kind": str(blocker.get("kind") or "").strip(),
                "source_test": str(blocker.get("kind") or "blocker").strip(),
                "summary": str(blocker.get("summary") or "").strip(),
                "details": str(blocker.get("details") or "").strip(),
                "paths": paths,
            }
        )
    for result in report.get("validation_results", []):
        if not isinstance(result, dict) or result.get("status") == "passed":
            continue
        failures.append(
            {
                "validation_id": str(result.get("id") or "validation").strip(),
                "source_test": str(result.get("id") or "validation").strip(),
                "excerpt": str(result.get("evidence") or "").strip(),
                "paths": [],
            }
        )
    for artifact in report.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        path = str(artifact.get("path") or "").strip()
        if path:
            focus_paths.append(path)
    if isinstance(task, dict):
        for key in ("owned_paths", "writes_existing_paths"):
            for value in task.get(key, []):
                normalized = str(value).strip()
                if normalized:
                    focus_paths.append(normalized)
        for output in task.get("expected_outputs", []):
            if not isinstance(output, dict):
                continue
            path = str(output.get("path") or "").strip()
            if path:
                focus_paths.append(path)
    deduped_paths = sorted({path for path in focus_paths if path and not path.startswith("runs/")})
    return {
        "source": "bundle_broad_retry",
        "summary": (
            "This is the mandatory first repair retry for the rejected bundle. "
            "Use the full project context available in the workspace, fix the reported error directly, "
            "and make the smallest coherent set of changes needed to get the task unstuck."
        ),
        "bundle_id": str(bundle.get("bundle_id") or "").strip(),
        "task_id": str(report.get("task_id") or "").strip(),
        "objective_id": str(report.get("objective_id") or "").strip(),
        "failures": failures,
        "focus_paths": deduped_paths,
        "allow_broadening_scope": True,
        "full_project_context": True,
    }


def build_bundle_run_state_repair_context(
    *,
    bundle: dict[str, Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    focus_paths: list[str] = []
    for blocker in report.get("blockers", []):
        if not isinstance(blocker, dict):
            continue
        paths = [
            str(value).strip()
            for value in blocker.get("related_paths", [])
            if isinstance(value, str) and str(value).strip()
        ]
        focus_paths.extend(paths)
        failures.append(
            {
                "kind": str(blocker.get("kind") or "").strip(),
                "source_test": str(blocker.get("kind") or "blocker").strip(),
                "summary": str(blocker.get("summary") or "").strip(),
                "details": str(blocker.get("details") or "").strip(),
                "paths": paths,
            }
        )
    for result in report.get("validation_results", []):
        if not isinstance(result, dict) or result.get("status") == "passed":
            continue
        failures.append(
            {
                "validation_id": str(result.get("id") or "validation").strip(),
                "source_test": str(result.get("id") or "validation").strip(),
                "excerpt": str(result.get("evidence") or "").strip(),
                "paths": [],
            }
        )
    for artifact in report.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        path = str(artifact.get("path") or "").strip()
        if path:
            focus_paths.append(path)
    return {
        "source": "run_state_repair",
        "summary": (
            "Repair stale or inconsistent run-local state for this task before retrying. "
            "Compare the repo, integration workspace, and task workspace for the listed paths, "
            "sync stale run-local copies forward, then complete only the remaining task work."
        ),
        "bundle_id": str(bundle.get("bundle_id") or "").strip(),
        "task_id": str(report.get("task_id") or "").strip(),
        "objective_id": str(report.get("objective_id") or "").strip(),
        "failures": failures,
        "focus_paths": sorted({path for path in focus_paths if path and not path.startswith("runs/")}),
    }


def classify_report_repair_class(report: dict[str, Any]) -> str:
    status = str(report.get("status") or "").strip()
    if status not in {"blocked", "failed", "needs_revision"}:
        return "objective_replan"
    blocker_kinds = {
        str(item.get("kind") or "").strip()
        for item in report.get("blockers", [])
        if isinstance(item, dict) and str(item.get("kind") or "").strip()
    }
    if blocker_kinds & STRUCTURAL_REPAIR_BLOCKER_KINDS:
        return "objective_replan"
    if blocker_kinds & RUN_STATE_REPAIR_BLOCKER_KINDS:
        return "run_state_repair"
    if blocker_kinds and blocker_kinds <= TASK_LOCAL_REPAIR_BLOCKER_KINDS:
        return "task_repair"
    failed_validations = [
        item for item in report.get("validation_results", []) if isinstance(item, dict) and item.get("status") != "passed"
    ]
    produced_artifacts = [
        item for item in report.get("artifacts", []) if isinstance(item, dict) and str(item.get("path") or "").strip()
    ]
    if failed_validations and produced_artifacts and not blocker_kinds:
        return "task_repair"
    return "objective_replan"


def choose_bundle_repair_strategy(
    project_root: Path,
    run_id: str,
    *,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    reports_dir = project_root / "runs" / run_id / "reports"
    task_repair_ids: set[str] = set()
    run_state_repair_ids: set[str] = set()
    blocked_or_failed_ids: set[str] = set()
    structural_failures = 0
    for task_id in bundle.get("included_tasks", []):
        report_path = reports_dir / f"{task_id}.json"
        if not report_path.exists():
            continue
        report = read_json(report_path)
        if report.get("status") in {"blocked", "failed", "needs_revision"}:
            blocked_or_failed_ids.add(task_id)
        repair_class = classify_report_repair_class(report)
        if repair_class == "task_repair":
            task_repair_ids.add(task_id)
        elif repair_class == "run_state_repair":
            run_state_repair_ids.add(task_id)
        elif report.get("status") in {"blocked", "failed", "needs_revision"}:
            structural_failures += 1
    if run_state_repair_ids and not structural_failures:
        return {"strategy": "run_state_repair", "task_ids": sorted(run_state_repair_ids)}
    if task_repair_ids and not structural_failures:
        return {"strategy": "task_repair", "task_ids": sorted(task_repair_ids)}
    if blocked_or_failed_ids:
        return {"strategy": "broad_task_repair", "task_ids": sorted(blocked_or_failed_ids)}
    return {"strategy": "objective_replan", "task_ids": []}


def polish_release_repair_attempts(project_root: Path, run_id: str) -> int:
    attempts = 0
    for event in load_optional_event_lines(project_root, run_id):
        if event.get("phase") != "polish" or event.get("event_type") != "phase.release_repair_requested":
            continue
        attempts += 1
    return attempts


def actionable_release_repair_diagnostics(phase_report: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(phase_report, dict) or phase_report.get("phase") != "polish":
        return []
    release_validation = phase_report.get("release_validation_summary")
    if not isinstance(release_validation, dict) or release_validation.get("status") != "failed":
        return []
    diagnostics = release_validation.get("failure_diagnostics")
    if not isinstance(diagnostics, list):
        return []
    return [
        item
        for item in diagnostics
        if isinstance(item, dict) and item.get("repairable") and item.get("owner_objective_id")
    ]


def build_polish_release_repair_context(
    *,
    phase_report: dict[str, Any],
    objective_id: str,
    objective_diagnostics: list[dict[str, Any]],
    included_task_ids: list[str],
) -> dict[str, Any]:
    release_validation = phase_report.get("release_validation_summary") or {}
    rejection_reasons: list[str] = []
    focus_paths: list[str] = []
    owner_capabilities: set[str] = set()
    for diagnostic in objective_diagnostics:
        source_test = str(diagnostic.get("source_test") or "release validation").strip()
        excerpt = str(diagnostic.get("excerpt") or "").strip()
        paths = [
            str(value).strip()
            for value in diagnostic.get("paths", [])
            if isinstance(value, str) and str(value).strip()
        ]
        focus_paths.extend(paths)
        owner_capability = str(diagnostic.get("owner_capability") or "").strip()
        if owner_capability:
            owner_capabilities.add(owner_capability)
        reason = f"{source_test}: {excerpt}" if excerpt else source_test
        if paths:
            reason = f"{reason} [paths: {', '.join(paths)}]"
        rejection_reasons.append(reason)
    return {
        "source": "polish_validation_checklist",
        "compact_prompt": True,
        "reason": (
            "Repair only the owned polish work needed for this objective so the polish "
            "validation checklist passes."
        ),
        "included_task_ids": included_task_ids,
        "rejection_reasons": rejection_reasons,
        "focus_paths": sorted({path for path in focus_paths if path}),
        "owner_capability": next(iter(owner_capabilities)) if len(owner_capabilities) == 1 else None,
        "release_validation_report_path": release_validation.get("report_path"),
    }


def polish_repair_context_path(project_root: Path, run_id: str, task_id: str) -> Path:
    return project_root / "runs" / run_id / "repair-contexts" / f"{task_id}.json"


def build_polish_task_repair_context(
    *,
    phase_report: dict[str, Any],
    task: dict[str, Any],
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    release_validation = phase_report.get("release_validation_summary") or {}
    failures = [
        {
            "task_id": diagnostic.get("task_id"),
            "source_test": diagnostic.get("source_test"),
            "excerpt": diagnostic.get("excerpt"),
            "paths": list(diagnostic.get("paths", [])),
            "category": diagnostic.get("category"),
        }
        for diagnostic in diagnostics
    ]
    return {
        "source": "polish_task_repair",
        "summary": (
            "Repair only the exact owned polish work needed for this task so its checklist items pass."
        ),
        "task_id": task["task_id"],
        "objective_id": task["objective_id"],
        "capability": task.get("capability"),
        "failures": failures,
        "release_validation_report_path": release_validation.get("report_path"),
    }


def dependent_task_ids(tasks: list[dict[str, Any]], seed_task_ids: set[str]) -> set[str]:
    selected = set(seed_task_ids)
    changed = True
    while changed:
        changed = False
        for task in tasks:
            task_id = str(task.get("task_id") or "").strip()
            if not task_id or task_id in selected:
                continue
            depends_on = {
                str(value).strip()
                for value in task.get("depends_on", [])
                if isinstance(value, str) and str(value).strip()
            }
            if depends_on & selected:
                selected.add(task_id)
                changed = True
    return selected


def attempt_polish_task_repairs(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    phase_report: dict[str, Any],
    diagnostics: list[dict[str, Any]],
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    max_concurrency: int,
) -> dict[str, Any]:
    grouped_by_objective: dict[str, list[dict[str, Any]]] = {}
    for diagnostic in diagnostics:
        objective_id = str(diagnostic.get("owner_objective_id") or "").strip()
        if objective_id:
            grouped_by_objective.setdefault(objective_id, []).append(diagnostic)

    objective_summaries: dict[str, Any] = {}
    repair_context_dir = ensure_dir(project_root / "runs" / run_id / "repair-contexts")
    for objective_id, objective_diagnostics in grouped_by_objective.items():
        objective_tasks = [
            task
            for task in phase_tasks(project_root / "runs" / run_id, phase)
            if task["objective_id"] == objective_id
        ]
        task_map = {task["task_id"]: task for task in objective_tasks}
        seed_task_ids = {
            str(item.get("task_id") or "").strip()
            for item in objective_diagnostics
            if str(item.get("task_id") or "").strip() in task_map
        }
        rerun_task_ids = dependent_task_ids(objective_tasks, seed_task_ids)
        for task_id in rerun_task_ids:
            task = task_map[task_id]
            task_diagnostics = [item for item in objective_diagnostics if str(item.get("task_id") or "").strip() == task_id]
            if task_diagnostics:
                write_json(
                    repair_context_dir / f"{task_id}.json",
                    build_polish_task_repair_context(
                        phase_report=phase_report,
                        task=task,
                        diagnostics=task_diagnostics,
                    ),
                )
            report_path = project_root / "runs" / run_id / "reports" / f"{task_id}.json"
            if report_path.exists():
                report_path.unlink()
        scheduler_summary = schedule_tasks(
            project_root,
            run_id,
            [task for task in objective_tasks if task["task_id"] in rerun_task_ids],
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            force=False,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
        )
        objective_summary = finalize_objective_bundle(
            project_root,
            run_id,
            phase,
            objective_id,
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
            scheduler_summary=scheduler_summary,
        )
        objective_summaries[objective_id] = {
            "repair_mode": "task_first",
            "rerun_task_ids": sorted(rerun_task_ids),
            "scheduler_summary": scheduler_summary,
            "objective_summary": objective_summary,
        }
        for task_id in rerun_task_ids:
            context_path = repair_context_dir / f"{task_id}.json"
            if context_path.exists():
                context_path.unlink()

    post_repair_report, _ = generate_phase_report(project_root, run_id)
    completed_status = "completed" if post_repair_report["release_validation_summary"]["status"] == "passed" else "failed"
    return {
        "status": completed_status,
        "objective_ids": sorted(grouped_by_objective),
        "objective_summaries": objective_summaries,
        "post_repair_report_path": f"runs/{run_id}/phase-reports/polish.json",
        "post_repair_report": post_repair_report,
        "repair_mode": "task_first",
    }


def attempt_bundle_task_repairs(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    objective_id: str,
    bundle: dict[str, Any],
    seed_task_ids: set[str],
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    max_concurrency: int,
) -> dict[str, Any]:
    objective_tasks = [
        task
        for task in phase_tasks(project_root / "runs" / run_id, phase)
        if task["objective_id"] == objective_id
    ]
    rerun_task_ids = dependent_task_ids(objective_tasks, seed_task_ids)
    reports_dir = project_root / "runs" / run_id / "reports"
    repair_context_dir = ensure_dir(project_root / "runs" / run_id / "repair-contexts")
    for task_id in rerun_task_ids:
        report_path = reports_dir / f"{task_id}.json"
        if task_id in seed_task_ids and report_path.exists():
            report = read_json(report_path)
            write_json(
                repair_context_dir / f"{task_id}.json",
                build_bundle_task_repair_context(bundle=bundle, report=report),
            )
        if report_path.exists():
            report_path.unlink()
    scheduler_summary = schedule_tasks(
        project_root,
        run_id,
        [task for task in objective_tasks if task["task_id"] in rerun_task_ids],
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        force=False,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
    )
    objective_summary = finalize_objective_bundle(
        project_root,
        run_id,
        phase,
        objective_id,
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
        allow_bundle_repair=False,
        scheduler_summary=scheduler_summary,
    )
    for task_id in rerun_task_ids:
        context_path = repair_context_dir / f"{task_id}.json"
        if context_path.exists():
            context_path.unlink()
    return {
        "repair_mode": "task_first",
        "rerun_task_ids": sorted(rerun_task_ids),
        "scheduler_summary": scheduler_summary,
        "objective_summary": objective_summary,
        "status": objective_summary.get("status"),
    }


def attempt_bundle_broad_repairs(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    objective_id: str,
    bundle: dict[str, Any],
    seed_task_ids: set[str],
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    max_concurrency: int,
) -> dict[str, Any]:
    objective_tasks = [
        task
        for task in phase_tasks(project_root / "runs" / run_id, phase)
        if task["objective_id"] == objective_id
    ]
    task_map = {str(task["task_id"]): task for task in objective_tasks}
    rerun_task_ids = dependent_task_ids(objective_tasks, seed_task_ids)
    reports_dir = project_root / "runs" / run_id / "reports"
    repair_context_dir = ensure_dir(project_root / "runs" / run_id / "repair-contexts")
    for task_id in rerun_task_ids:
        report_path = reports_dir / f"{task_id}.json"
        if task_id in seed_task_ids and report_path.exists():
            report = read_json(report_path)
            write_json(
                repair_context_dir / f"{task_id}.json",
                build_bundle_broad_retry_context(
                    bundle=bundle,
                    report=report,
                    task=task_map.get(task_id),
                ),
            )
        if report_path.exists():
            report_path.unlink()
    scheduler_summary = schedule_tasks(
        project_root,
        run_id,
        [task for task in objective_tasks if task["task_id"] in rerun_task_ids],
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        force=False,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
    )
    objective_summary = finalize_objective_bundle(
        project_root,
        run_id,
        phase,
        objective_id,
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
        allow_bundle_repair=False,
        scheduler_summary=scheduler_summary,
    )
    for task_id in rerun_task_ids:
        context_path = repair_context_dir / f"{task_id}.json"
        if context_path.exists():
            context_path.unlink()
    return {
        "repair_mode": "broad_task_first",
        "rerun_task_ids": sorted(rerun_task_ids),
        "scheduler_summary": scheduler_summary,
        "objective_summary": objective_summary,
        "status": objective_summary.get("status"),
    }


def attempt_bundle_run_state_repairs(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    objective_id: str,
    bundle: dict[str, Any],
    seed_task_ids: set[str],
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    max_concurrency: int,
) -> dict[str, Any]:
    objective_tasks = [
        task
        for task in phase_tasks(project_root / "runs" / run_id, phase)
        if task["objective_id"] == objective_id
    ]
    rerun_task_ids = dependent_task_ids(objective_tasks, seed_task_ids)
    reports_dir = project_root / "runs" / run_id / "reports"
    repair_context_dir = ensure_dir(project_root / "runs" / run_id / "repair-contexts")
    for task_id in rerun_task_ids:
        report_path = reports_dir / f"{task_id}.json"
        if task_id in seed_task_ids and report_path.exists():
            report = read_json(report_path)
            write_json(
                repair_context_dir / f"{task_id}.json",
                build_bundle_run_state_repair_context(bundle=bundle, report=report),
            )
        if report_path.exists():
            report_path.unlink()
    scheduler_summary = schedule_tasks(
        project_root,
        run_id,
        [task for task in objective_tasks if task["task_id"] in rerun_task_ids],
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        force=False,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
    )
    objective_summary = finalize_objective_bundle(
        project_root,
        run_id,
        phase,
        objective_id,
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
        allow_bundle_repair=False,
        scheduler_summary=scheduler_summary,
    )
    for task_id in rerun_task_ids:
        context_path = repair_context_dir / f"{task_id}.json"
        if context_path.exists():
            context_path.unlink()
    return {
        "repair_mode": "run_state_first",
        "rerun_task_ids": sorted(rerun_task_ids),
        "scheduler_summary": scheduler_summary,
        "objective_summary": objective_summary,
        "status": objective_summary.get("status"),
    }


def attempt_polish_objective_repair(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    phase_report: dict[str, Any],
    diagnostics: list[dict[str, Any]],
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    max_concurrency: int,
) -> dict[str, Any] | None:
    grouped_diagnostics: dict[str, list[dict[str, Any]]] = {}
    for diagnostic in diagnostics:
        objective_id = str(diagnostic["owner_objective_id"]).strip()
        grouped_diagnostics.setdefault(objective_id, []).append(diagnostic)

    objective_summaries: dict[str, Any] = {}
    for objective_id in sorted(grouped_diagnostics):
        objective_tasks = [
            task
            for task in phase_tasks(project_root / "runs" / run_id, phase)
            if task["objective_id"] == objective_id
        ]
        repair_context = build_polish_release_repair_context(
            phase_report=phase_report,
            objective_id=objective_id,
            objective_diagnostics=grouped_diagnostics[objective_id],
            included_task_ids=[task["task_id"] for task in objective_tasks],
        )
        try:
            plan_summary = plan_objective(
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
            refreshed_tasks = [
                task
                for task in phase_tasks(project_root / "runs" / run_id, phase)
                if task["objective_id"] == objective_id
            ]
            scheduler_summary = schedule_tasks(
                project_root,
                run_id,
                refreshed_tasks,
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                force=False,
                timeout_seconds=timeout_seconds,
                max_concurrency=max_concurrency,
            )
            objective_summary = finalize_objective_bundle(
                project_root,
                run_id,
                phase,
                objective_id,
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                timeout_seconds=timeout_seconds,
                max_concurrency=max_concurrency,
                scheduler_summary=scheduler_summary,
            )
            objective_summaries[objective_id] = {
                "repair_mode": "objective_fallback",
                "plan_summary": plan_summary,
                "scheduler_summary": scheduler_summary,
                "objective_summary": objective_summary,
            }
        except BaseException as exc:
            record_event(
                project_root,
                run_id,
                phase=phase,
                activity_id=None,
                event_type="phase.release_repair_failed",
                message=f"Polish release repair failed for objective {objective_id}.",
                payload={"objective_id": objective_id, "error": str(exc)},
            )
            return {
                "status": "failed",
                "objective_ids": sorted(grouped_diagnostics),
                "error": str(exc),
                "objective_summaries": objective_summaries,
                "repair_mode": "objective_fallback",
            }

    post_repair_report, _ = generate_phase_report(project_root, run_id)
    completed_status = "completed" if post_repair_report["release_validation_summary"]["status"] == "passed" else "failed"
    return {
        "status": completed_status,
        "objective_ids": sorted(grouped_diagnostics),
        "objective_summaries": objective_summaries,
        "post_repair_report_path": f"runs/{run_id}/phase-reports/polish.json",
        "post_repair_report": post_repair_report,
        "repair_mode": "objective_fallback",
    }


def attempt_polish_release_repair(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    phase_report: dict[str, Any],
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    max_concurrency: int,
) -> dict[str, Any] | None:
    if phase != "polish":
        return None
    diagnostics = actionable_release_repair_diagnostics(phase_report)
    if not diagnostics:
        return None
    if polish_release_repair_attempts(project_root, run_id) >= MAX_POLISH_RELEASE_REPAIR_ATTEMPTS:
        record_event(
            project_root,
            run_id,
            phase=phase,
            activity_id=None,
            event_type="phase.release_repair_exhausted",
            message="Polish validation checklist still fails and the repair budget is exhausted.",
            payload={
                "diagnostic_count": len(diagnostics),
                "objective_ids": sorted({item["owner_objective_id"] for item in diagnostics}),
            },
        )
        return None

    grouped_diagnostics: dict[str, list[dict[str, Any]]] = {}
    for diagnostic in diagnostics:
        objective_id = str(diagnostic["owner_objective_id"]).strip()
        grouped_diagnostics.setdefault(objective_id, []).append(diagnostic)

    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=None,
        event_type="phase.release_repair_requested",
        message="Retrying polish objectives after the validation checklist failed.",
        payload={
            "objective_ids": sorted(grouped_diagnostics),
            "diagnostic_count": len(diagnostics),
            "report_path": phase_report.get("release_validation_summary", {}).get("report_path"),
        },
    )

    task_level_diagnostics = [item for item in diagnostics if str(item.get("task_id") or "").strip()]
    if task_level_diagnostics:
        task_repair_summary = attempt_polish_task_repairs(
            project_root,
            run_id,
            phase=phase,
            phase_report=phase_report,
            diagnostics=task_level_diagnostics,
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
        )
        post_repair_report = task_repair_summary.get("post_repair_report")
        if isinstance(post_repair_report, dict) and post_repair_report.get("release_validation_summary", {}).get("status") == "passed":
            record_event(
                project_root,
                run_id,
                phase=phase,
                activity_id=None,
                event_type="phase.release_repair_completed",
                message="Polish task-level repair completed and the validation checklist passed.",
                payload={"repair_mode": "task_first"},
            )
            return task_repair_summary
        refreshed_diagnostics = actionable_release_repair_diagnostics(post_repair_report if isinstance(post_repair_report, dict) else phase_report)
        diagnostics = refreshed_diagnostics or diagnostics

    objective_repair_summary = attempt_polish_objective_repair(
        project_root,
        run_id,
        phase=phase,
        phase_report=post_repair_report if task_level_diagnostics and isinstance(post_repair_report, dict) else phase_report,
        diagnostics=diagnostics,
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
    )
    if objective_repair_summary is None:
        return None
    post_repair_report = objective_repair_summary.get("post_repair_report")
    completed_status = "completed" if isinstance(post_repair_report, dict) and post_repair_report["release_validation_summary"]["status"] == "passed" else "failed"
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=None,
        event_type=f"phase.release_repair_{completed_status}",
        message=(
            "Polish release repair completed and the integrated release gate passed."
            if completed_status == "completed"
            else "Polish release repair completed, but the integrated release gate still fails."
        ),
        payload={
            "objective_ids": sorted(grouped_diagnostics),
            "release_validation_status": post_repair_report["release_validation_summary"]["status"] if isinstance(post_repair_report, dict) else "failed",
            "repair_mode": objective_repair_summary.get("repair_mode"),
        },
    )
    return objective_repair_summary


def attempt_bundle_repair(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    objective_id: str,
    bundle: dict[str, Any],
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    max_concurrency: int,
) -> dict[str, Any] | None:
    bundle_id = str(bundle.get("bundle_id", "")).strip()
    if not bundle_id:
        return None
    if bundle_repair_attempts(
        project_root,
        run_id,
        phase=phase,
        objective_id=objective_id,
        bundle_id=bundle_id,
    ) >= MAX_OBJECTIVE_BUNDLE_REPAIR_ATTEMPTS:
        return None

    repair_plan = choose_bundle_repair_strategy(
        project_root,
        run_id,
        bundle=bundle,
    )
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=None,
        event_type="bundle.repair_requested",
        message=f"Retrying objective {objective_id} after bundle {bundle_id} rejection.",
        payload={
            "objective_id": objective_id,
            "bundle_id": bundle_id,
            "rejection_reasons": list(bundle.get("rejection_reasons", [])),
            "repair_strategy": repair_plan["strategy"],
        },
    )

    try:
        if repair_plan["strategy"] == "broad_task_repair":
            broad_repair_summary = attempt_bundle_broad_repairs(
                project_root,
                run_id,
                phase=phase,
                objective_id=objective_id,
                bundle=bundle,
                seed_task_ids=set(repair_plan["task_ids"]),
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                timeout_seconds=timeout_seconds,
                max_concurrency=max_concurrency,
            )
            if broad_repair_summary["status"] == "accepted":
                record_event(
                    project_root,
                    run_id,
                    phase=phase,
                    activity_id=None,
                    event_type="bundle.repair_completed",
                    message=f"Bundle repair for {bundle_id} succeeded via broad task repair.",
                    payload={
                        "objective_id": objective_id,
                        "bundle_id": bundle_id,
                        "repair_mode": "broad_task_first",
                        "rerun_task_ids": broad_repair_summary.get("rerun_task_ids", []),
                    },
                )
                return broad_repair_summary["objective_summary"]
        if repair_plan["strategy"] == "run_state_repair":
            run_state_repair_summary = attempt_bundle_run_state_repairs(
                project_root,
                run_id,
                phase=phase,
                objective_id=objective_id,
                bundle=bundle,
                seed_task_ids=set(repair_plan["task_ids"]),
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                timeout_seconds=timeout_seconds,
                max_concurrency=max_concurrency,
            )
            if run_state_repair_summary["status"] == "accepted":
                record_event(
                    project_root,
                    run_id,
                    phase=phase,
                    activity_id=None,
                    event_type="bundle.repair_completed",
                    message=f"Bundle repair for {bundle_id} succeeded via run-state repair.",
                    payload={
                        "objective_id": objective_id,
                        "bundle_id": bundle_id,
                        "repair_mode": "run_state_first",
                        "rerun_task_ids": run_state_repair_summary.get("rerun_task_ids", []),
                    },
                )
                return run_state_repair_summary["objective_summary"]
        if repair_plan["strategy"] == "task_repair":
            task_repair_summary = attempt_bundle_task_repairs(
                project_root,
                run_id,
                phase=phase,
                objective_id=objective_id,
                bundle=bundle,
                seed_task_ids=set(repair_plan["task_ids"]),
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                timeout_seconds=timeout_seconds,
                max_concurrency=max_concurrency,
            )
            if task_repair_summary["status"] == "accepted":
                record_event(
                    project_root,
                    run_id,
                    phase=phase,
                    activity_id=None,
                    event_type="bundle.repair_completed",
                    message=f"Bundle repair for {bundle_id} succeeded via task repair.",
                    payload={
                        "objective_id": objective_id,
                        "bundle_id": bundle_id,
                        "repair_mode": "task_first",
                        "rerun_task_ids": task_repair_summary.get("rerun_task_ids", []),
                    },
                )
                return task_repair_summary["objective_summary"]
        repair_context = build_bundle_repair_context(
            phase=phase,
            objective_id=objective_id,
            bundle=bundle,
        )
        plan_summary = plan_objective(
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
        objective_tasks = [
            task
            for task in phase_tasks(project_root / "runs" / run_id, phase)
            if task["objective_id"] == objective_id
        ]
        scheduler_summary = schedule_tasks(
            project_root,
            run_id,
            objective_tasks,
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            force=False,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
        )
        repaired_summary = finalize_objective_bundle(
            project_root,
            run_id,
            phase,
            objective_id,
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
            allow_bundle_repair=False,
            scheduler_summary=scheduler_summary,
        )
    except BaseException as exc:
        record_event(
            project_root,
            run_id,
            phase=phase,
            activity_id=None,
            event_type="bundle.repair_failed",
            message=f"Bundle repair for {bundle_id} failed.",
            payload={
                "objective_id": objective_id,
                "bundle_id": bundle_id,
                "error": str(exc),
            },
        )
        return None

    if repaired_summary["status"] == "accepted":
        record_event(
            project_root,
            run_id,
            phase=phase,
            activity_id=None,
            event_type="bundle.repair_completed",
            message=f"Bundle repair for {bundle_id} succeeded.",
            payload={
                "objective_id": objective_id,
                "bundle_id": bundle_id,
                "plan_recovery_action": plan_summary.get("recovery_action"),
                "repair_mode": "objective_replan",
            },
        )
    else:
        record_event(
            project_root,
            run_id,
            phase=phase,
            activity_id=None,
            event_type="bundle.repair_failed",
            message=f"Bundle repair for {bundle_id} did not produce an accepted bundle.",
            payload={
                "objective_id": objective_id,
                "bundle_id": bundle_id,
                "status": repaired_summary.get("status"),
                "scheduler_summary": scheduler_summary,
                "rejection_reasons": repaired_summary.get("rejection_reasons", []),
            },
        )
    return repaired_summary


def schedule_tasks(
    project_root: Path,
    run_id: str,
    tasks: list[dict[str, Any]],
    *,
    sandbox_mode: str,
    codex_path: str,
    force: bool,
    timeout_seconds: int | None,
    max_concurrency: int,
) -> dict[str, Any]:
    reconcile_for_command(project_root, run_id, apply=True)
    run_dir = project_root / "runs" / run_id
    initialize_live_run(project_root, run_id)
    reports_dir = run_dir / "reports"
    tasks_by_id = {task["task_id"]: task for task in tasks}
    all_reports = {path.stem: read_json(path) for path in sorted(reports_dir.glob("*.json"))}
    if force:
        completed: set[str] = set()
        failed: set[str] = set()
    else:
        completed = {task_id for task_id, report in all_reports.items() if report["status"] == "ready_for_bundle_review"}
        # A blocked task report is recoverable: the scheduler should retry it on
        # a later resume after planning inputs, handoffs, or other external
        # blockers have changed. Treat only non-recoverable terminal states as
        # failed here.
        failed = {
            task_id
            for task_id, report in all_reports.items()
            if report["status"] not in {"ready_for_bundle_review", "blocked"}
        }
    pending = dict(tasks_by_id)
    executed: list[dict[str, Any]] = []
    skipped_existing: list[str] = []
    skipped_dependency: dict[str, list[str]] = {}
    unresolved_dependencies: dict[str, list[str]] = {}
    blocked_handoffs: dict[str, list[str]] = {}
    stale_tasks: dict[str, list[str]] = {}
    failures: list[dict[str, str]] = []
    running: dict[str, dict[str, Any]] = {}
    warning_events_emitted: set[tuple[str, str]] = set()
    handoff_events_emitted: set[tuple[str, str, tuple[str, ...]]] = set()
    forced_serialization: dict[str, tuple[str, str]] = {}
    max_concurrency = max(1, max_concurrency)
    handoffs_by_id = refresh_handoffs_for_phase(project_root, run_id, tasks[0]["phase"], tasks_by_id) if tasks else {}

    def emit_parallel_warning(task: dict[str, Any], code: str, message: str) -> None:
        marker = (task["task_id"], code)
        if marker in warning_events_emitted:
            return
        warning_events_emitted.add(marker)
        record_event(
            project_root,
            run_id,
            phase=task["phase"],
            activity_id=task["task_id"],
            event_type="task.parallel_warning",
            message=f"Task {task['task_id']} will run serialized: {message}",
            payload={"code": code, "reason": message},
        )

    def set_queue_state(
        task: dict[str, Any],
        *,
        queue_position: int | None,
        current_activity: str,
        warnings: list[dict[str, str]] | None = None,
        parallel_execution_granted: bool | None = None,
        parallel_fallback_reason: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "status": "queued",
            "progress_stage": "queued",
            "current_activity": current_activity,
            "dependency_blockers": [],
            "dependency_blocker_fingerprint": None,
            "handoff_blocker_fingerprint": None,
            "queue_position": queue_position,
            "status_reason": None,
            "parallel_execution_requested": parallel_requested(task),
        }
        if warnings is not None:
            payload["warnings"] = warnings
        if parallel_execution_granted is not None:
            payload["parallel_execution_granted"] = parallel_execution_granted
        if parallel_fallback_reason is not None:
            payload["parallel_fallback_reason"] = parallel_fallback_reason
        update_activity(project_root, run_id, task["task_id"], **payload)

    def blockers_fingerprint(blockers: list[str]) -> str | None:
        if not blockers:
            return None
        return "|".join(sorted(set(blockers)))

    def read_task_activity(task_id: str) -> dict[str, Any]:
        return load_optional_json(activity_path(run_dir, task_id)) or {}

    def queue_state_changed(task_id: str, *, queue_position: int | None, current_activity: str) -> bool:
        previous = read_task_activity(task_id)
        return (
            previous.get("status") != "queued"
            or previous.get("queue_position") != queue_position
            or previous.get("current_activity") != current_activity
        )

    def emit_resolution_events(task: dict[str, Any], previous: dict[str, Any]) -> None:
        dependency_fingerprint = previous.get("dependency_blocker_fingerprint")
        if dependency_fingerprint:
            record_event(
                project_root,
                run_id,
                phase=task["phase"],
                activity_id=task["task_id"],
                event_type="task.dependencies_resolved",
                message=f"Task {task['task_id']} has all task dependencies resolved.",
                payload={"resolved_dependency_blockers": dependency_fingerprint.split("|")},
            )
        handoff_fingerprint = previous.get("handoff_blocker_fingerprint")
        if handoff_fingerprint:
            record_event(
                project_root,
                run_id,
                phase=task["phase"],
                activity_id=task["task_id"],
                event_type="task.handoffs_resolved",
                message=f"Task {task['task_id']} has all blocking collaboration handoffs resolved.",
                payload={"resolved_handoff_blockers": handoff_fingerprint.split("|")},
            )

    def set_dependency_wait_state(task: dict[str, Any], blockers: list[str]) -> None:
        previous = read_task_activity(task["task_id"])
        fingerprint = blockers_fingerprint(blockers)
        update_activity(
            project_root,
            run_id,
            task["task_id"],
            status="waiting_dependencies",
            progress_stage="waiting_dependencies",
            current_activity="Waiting on dependency completion.",
            dependency_blockers=blockers,
            dependency_blocker_fingerprint=fingerprint,
            handoff_blocker_fingerprint=None,
            queue_position=None,
            status_reason=None,
        )
        if (
            previous.get("status") != "waiting_dependencies"
            or previous.get("dependency_blocker_fingerprint") != fingerprint
            or previous.get("current_activity") != "Waiting on dependency completion."
            or previous.get("status_reason") is not None
        ):
            record_event(
                project_root,
                run_id,
                phase=task["phase"],
                activity_id=task["task_id"],
                event_type="task.waiting_dependencies",
                message=f"Task {task['task_id']} is waiting on dependencies.",
                payload={"dependency_blockers": blockers},
            )

    def set_waiting_handoffs_state(task: dict[str, Any], handoffs: list[dict[str, Any]]) -> None:
        previous = read_task_activity(task["task_id"])
        blocker_ids = [handoff["handoff_id"] for handoff in handoffs]
        fingerprint = blockers_fingerprint(blocker_ids)
        reason = "Waiting on blocking collaboration handoff."
        update_activity(
            project_root,
            run_id,
            task["task_id"],
            status="waiting_dependencies",
            progress_stage="waiting_dependencies",
            current_activity="Waiting on collaboration handoff completion.",
            dependency_blockers=blocker_ids,
            dependency_blocker_fingerprint=None,
            handoff_blocker_fingerprint=fingerprint,
            queue_position=None,
            status_reason=reason,
        )
        if (
            previous.get("status") != "waiting_dependencies"
            or previous.get("handoff_blocker_fingerprint") != fingerprint
            or previous.get("current_activity") != "Waiting on collaboration handoff completion."
            or previous.get("status_reason") != reason
        ):
            record_event(
                project_root,
                run_id,
                phase=task["phase"],
                activity_id=task["task_id"],
                event_type="task.waiting_handoffs",
                message=f"Task {task['task_id']} is waiting on collaboration handoffs.",
                payload={
                    "handoff_ids": blocker_ids,
                    "reasons": [handoff.get("status_reason") for handoff in handoffs],
                },
            )

    def launch_task(
        pool: ThreadPoolExecutor,
        task: dict[str, Any],
        *,
        granted: bool,
        warning_code: str | None = None,
        warning_message: str | None = None,
    ) -> None:
        runtime_warnings = []
        if warning_code and warning_message:
            runtime_warnings.append(warning(warning_code, warning_message))
            emit_parallel_warning(task, warning_code, warning_message)
        future = pool.submit(
            execute_task,
            project_root,
            run_id,
            task["task_id"],
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            runtime=TaskExecutionRuntime(
                parallel_execution_requested=parallel_requested(task),
                parallel_execution_granted=granted,
                parallel_fallback_reason=warning_message,
                runtime_warnings=runtime_warnings,
            ),
        )
        running[task["task_id"]] = {
            "future": future,
            "task": task,
            "serialized": not granted,
        }
        pending.pop(task["task_id"], None)

    for task in tasks:
        existing_activity = load_optional_json(activity_path(run_dir, task["task_id"]))
        if existing_activity is None:
            ensure_activity(
                project_root,
                run_id,
                activity_id=task["task_id"],
                kind="task_execution",
                entity_id=task["task_id"],
                phase=task["phase"],
                objective_id=task["objective_id"],
                display_name=task["task_id"],
                assigned_role=task["assigned_role"],
                status="waiting_dependencies",
                progress_stage="waiting_dependencies",
                current_activity="Waiting for scheduler.",
                prompt_path=None,
                stdout_path=f"runs/{run_id}/executions/{task['task_id']}.stdout.jsonl",
                stderr_path=f"runs/{run_id}/executions/{task['task_id']}.stderr.log",
                output_path=f"runs/{run_id}/reports/{task['task_id']}.json",
                dependency_blockers=list(task["depends_on"]),
                parallel_execution_requested=parallel_requested(task),
            )
            record_event(
                project_root,
                run_id,
                phase=task["phase"],
                activity_id=task["task_id"],
                event_type="task.discovered",
                message=f"Discovered task {task['task_id']} for phase scheduling.",
                payload={"depends_on": list(task["depends_on"]), "objective_id": task["objective_id"]},
            )

    if not force:
        for task_id in list(pending):
            if task_id not in completed:
                continue
            skipped_existing.append(task_id)
            report = all_reports[task_id]
            update_activity(
                project_root,
                run_id,
                task_id,
                status=report["status"],
                progress_stage=report["status"],
                current_activity="Using existing task report.",
                dependency_blockers=[],
                dependency_blocker_fingerprint=None,
                handoff_blocker_fingerprint=None,
                queue_position=None,
                status_reason=None,
            )
            record_event(
                project_root,
                run_id,
                phase=tasks_by_id[task_id]["phase"],
                activity_id=task_id,
                event_type="task.skipped_existing",
                message=f"Skipped {task_id} because an existing completed report is present.",
                payload={"report_status": report["status"]},
            )
            pending.pop(task_id)

    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        while pending or running:
            progressed = False
            handoffs_by_id = refresh_handoffs_for_phase(project_root, run_id, tasks[0]["phase"], tasks_by_id) if tasks else {}
            stale_notifications = stale_task_notifications(project_root, run_id, phase=tasks[0]["phase"] if tasks else None)

            for task_id, info in list(running.items()):
                future: Future[dict[str, Any]] = info["future"]
                if not future.done():
                    continue
                progressed = True
                task = info["task"]
                del running[task_id]
                try:
                    execution_summary = future.result()
                except ExecutorError as exc:
                    failures.append({"task_id": task_id, "message": str(exc)})
                    failed.add(task_id)
                    record_event(
                        project_root,
                        run_id,
                        phase=task["phase"],
                        activity_id=task_id,
                        event_type="task.failed",
                        message=f"Task {task_id} failed during execution.",
                        payload={"error": str(exc)},
                    )
                    continue
                executed.append(execution_summary)
                update_activity(
                    project_root,
                    run_id,
                    task_id,
                    status=execution_summary["status"],
                    progress_stage=execution_summary["status"],
                    current_activity=f"Execution finished with status {execution_summary['status']}.",
                    queue_position=None,
                    dependency_blockers=[],
                    dependency_blocker_fingerprint=None,
                    handoff_blocker_fingerprint=None,
                    status_reason=None,
                    stdout_path=execution_summary.get("stdout_path"),
                    stderr_path=execution_summary.get("stderr_path"),
                    output_path=execution_summary.get("report_path"),
                    warnings=list(execution_summary.get("runtime_warnings", [])),
                    parallel_execution_requested=execution_summary.get("parallel_execution_requested", False),
                    parallel_execution_granted=execution_summary.get("parallel_execution_granted", False),
                    parallel_fallback_reason=execution_summary.get("parallel_fallback_reason"),
                    workspace_path=execution_summary.get("workspace_path"),
                    branch_name=execution_summary.get("branch_name"),
                )
                if execution_summary["status"] == "ready_for_bundle_review":
                    completed.add(task_id)
                elif execution_summary["status"] != "blocked":
                    failed.add(task_id)

            ready: list[dict[str, Any]] = []
            for task_id, task in list(pending.items()):
                task_stale_notifications = stale_notifications.get(task_id, [])
                if task_stale_notifications:
                    reasons = [item["reason"] for item in task_stale_notifications]
                    stale_tasks[task_id] = reasons
                    update_activity(
                        project_root,
                        run_id,
                        task_id,
                        status="needs_revision",
                        progress_stage="needs_revision",
                        current_activity="Inputs stale after approved change request.",
                        dependency_blockers=[],
                        dependency_blocker_fingerprint=None,
                        handoff_blocker_fingerprint=None,
                        queue_position=None,
                        status_reason="; ".join(reasons),
                    )
                    record_event(
                        project_root,
                        run_id,
                        phase=task["phase"],
                        activity_id=task_id,
                        event_type="task.stale_inputs",
                        message=f"Task {task_id} is stale after an approved change request.",
                        payload={"reasons": reasons},
                    )
                    pending.pop(task_id)
                    continue
                failed_deps = [dependency for dependency in task["depends_on"] if dependency in failed or dependency in skipped_dependency]
                if failed_deps:
                    skipped_dependency[task_id] = failed_deps
                    update_activity(
                        project_root,
                        run_id,
                        task_id,
                        status="blocked",
                        progress_stage="blocked",
                        current_activity="Blocked by failed dependencies.",
                        dependency_blockers=failed_deps,
                        dependency_blocker_fingerprint=blockers_fingerprint(failed_deps),
                        handoff_blocker_fingerprint=None,
                        queue_position=None,
                    )
                    record_event(
                        project_root,
                        run_id,
                        phase=task["phase"],
                        activity_id=task_id,
                        event_type="task.blocked",
                        message=f"Task {task_id} is blocked by failed dependencies.",
                        payload={"dependency_blockers": failed_deps},
                    )
                    pending.pop(task_id)
                    continue

                unmet_deps = [dependency for dependency in task["depends_on"] if dependency not in completed]
                if unmet_deps:
                    set_dependency_wait_state(task, unmet_deps)
                    continue
                task_handoffs = blocking_handoffs_for_task(task, handoffs_by_id)
                blocked_task_handoffs = [
                    handoff for handoff in task_handoffs if handoff.get("status") == HANDOFF_BLOCKED
                ]
                waiting_task_handoffs = [
                    handoff
                    for handoff in task_handoffs
                    if handoff.get("status") not in {"satisfied", HANDOFF_BLOCKED}
                ]
                if blocked_task_handoffs:
                    blocker_ids = [handoff["handoff_id"] for handoff in blocked_task_handoffs]
                    blocked_handoffs[task_id] = blocker_ids
                    reason = "; ".join(
                        handoff.get("status_reason") or f"{handoff['handoff_id']} is blocked"
                        for handoff in blocked_task_handoffs
                    )
                    update_activity(
                        project_root,
                        run_id,
                        task_id,
                        status="blocked",
                        progress_stage="blocked",
                        current_activity="Blocked by collaboration handoff.",
                        dependency_blockers=blocker_ids,
                        dependency_blocker_fingerprint=None,
                        handoff_blocker_fingerprint=blockers_fingerprint(blocker_ids),
                        queue_position=None,
                        status_reason=reason,
                    )
                    marker = (task_id, "task.handoff_blocked", tuple(blocker_ids))
                    if marker not in handoff_events_emitted:
                        handoff_events_emitted.add(marker)
                        record_event(
                            project_root,
                            run_id,
                            phase=task["phase"],
                            activity_id=task_id,
                            event_type="task.handoff_blocked",
                            message=f"Task {task_id} is blocked by collaboration handoffs.",
                            payload={
                                "handoff_ids": blocker_ids,
                                "reasons": [handoff.get("status_reason") for handoff in blocked_task_handoffs],
                            },
                        )
                    pending.pop(task_id)
                    continue
                if waiting_task_handoffs:
                    set_waiting_handoffs_state(task, waiting_task_handoffs)
                    continue
                ready.append(task)

            if not ready and not running and pending:
                for task_id, task in pending.items():
                    blockers = [dependency for dependency in task["depends_on"] if dependency not in completed]
                    blockers.extend(
                        handoff["handoff_id"]
                        for handoff in blocking_handoffs_for_task(task, handoffs_by_id)
                        if handoff.get("status") != "satisfied"
                    )
                    unresolved_dependencies[task_id] = blockers
                break
            if not ready and running:
                wait([info["future"] for info in running.values()], timeout=0.1, return_when=FIRST_COMPLETED)
                continue

            ordered_ready = sorted(ready, key=lambda item: item["task_id"])
            serialized_running = any(info["serialized"] for info in running.values())
            candidate_running = [info["task"] for info in running.values() if not info["serialized"]]
            ready_parallel: list[dict[str, Any]] = []
            ready_serialized: list[tuple[dict[str, Any], str, str]] = []
            for task in ordered_ready:
                serialized_reason = forced_serialization.get(task["task_id"])
                if serialized_reason is not None:
                    ready_serialized.append((task, serialized_reason[0], serialized_reason[1]))
                    continue
                is_safe, warning_code, warning_message = classify_parallel_safety(task, running_tasks=candidate_running)
                if is_safe:
                    ready_parallel.append(task)
                    candidate_running.append(task)
                else:
                    reason = (
                        warning_code or "serialize_only",
                        warning_message or "Task must run serialized.",
                    )
                    forced_serialization[task["task_id"]] = reason
                    ready_serialized.append(
                        (
                            task,
                            reason[0],
                            reason[1],
                        )
                    )

            queue_index = 1
            if serialized_running:
                for task in ordered_ready:
                    previous = read_task_activity(task["task_id"])
                    emit_resolution_events(task, previous)
                    changed = queue_state_changed(
                        task["task_id"],
                        queue_position=queue_index,
                        current_activity="Waiting for the serialized execution lane to clear.",
                    )
                    set_queue_state(
                        task,
                        queue_position=queue_index,
                        current_activity="Waiting for the serialized execution lane to clear.",
                    )
                    if changed:
                        record_event(
                            project_root,
                            run_id,
                            phase=task["phase"],
                            activity_id=task["task_id"],
                            event_type="task.serialized_lane_wait",
                            message=f"Task {task['task_id']} is waiting because a serialized task is currently running.",
                            payload={},
                        )
                    queue_index += 1
                wait([info["future"] for info in running.values()], timeout=0.1, return_when=FIRST_COMPLETED)
                continue

            started_any = False
            slots = max(0, max_concurrency - len(running))
            for task in ready_parallel[:slots]:
                previous = read_task_activity(task["task_id"])
                emit_resolution_events(task, previous)
                changed = queue_state_changed(
                    task["task_id"],
                    queue_position=queue_index,
                    current_activity="Queued for parallel execution.",
                )
                set_queue_state(
                    task,
                    queue_position=queue_index,
                    current_activity="Queued for parallel execution.",
                    parallel_execution_granted=True,
                )
                if changed:
                    record_event(
                        project_root,
                        run_id,
                        phase=task["phase"],
                        activity_id=task["task_id"],
                        event_type="task.queued",
                        message=f"Queued task {task['task_id']} for parallel execution.",
                        payload={"queue_position": queue_index},
                    )
                launch_task(pool, task, granted=True)
                queue_index += 1
                started_any = True

            for task in ready_parallel[slots:]:
                previous = read_task_activity(task["task_id"])
                emit_resolution_events(task, previous)
                changed = queue_state_changed(
                    task["task_id"],
                    queue_position=queue_index,
                    current_activity="Queued for parallel execution.",
                )
                set_queue_state(
                    task,
                    queue_position=queue_index,
                    current_activity="Queued for parallel execution.",
                    parallel_execution_granted=True,
                )
                if changed:
                    record_event(
                        project_root,
                        run_id,
                        phase=task["phase"],
                        activity_id=task["task_id"],
                        event_type="task.queued",
                        message=f"Queued task {task['task_id']} for parallel execution.",
                        payload={"queue_position": queue_index},
                    )
                queue_index += 1

            for task, warning_code, warning_message in ready_serialized:
                previous = read_task_activity(task["task_id"])
                emit_resolution_events(task, previous)
                changed = queue_state_changed(
                    task["task_id"],
                    queue_position=queue_index,
                    current_activity="Queued for serialized execution.",
                )
                set_queue_state(
                    task,
                    queue_position=queue_index,
                    current_activity="Queued for serialized execution.",
                    warnings=[warning(warning_code, warning_message)],
                    parallel_execution_granted=False,
                    parallel_fallback_reason=warning_message,
                )
                emit_parallel_warning(task, warning_code, warning_message)
                if changed:
                    record_event(
                        project_root,
                        run_id,
                        phase=task["phase"],
                        activity_id=task["task_id"],
                        event_type="task.queued",
                        message=f"Queued task {task['task_id']} for serialized execution.",
                        payload={"queue_position": queue_index, "reason": warning_message},
                    )
                queue_index += 1

            if not started_any and not running and ready_serialized:
                task, warning_code, warning_message = ready_serialized[0]
                launch_task(pool, task, granted=False, warning_code=warning_code, warning_message=warning_message)
                started_any = True

            if not started_any and running:
                wait([info["future"] for info in running.values()], timeout=0.1, return_when=FIRST_COMPLETED)
            elif not started_any and not progressed:
                time.sleep(0.05)

    return {
        "phase": active_phase(run_dir),
        "executed": executed,
        "skipped_existing": skipped_existing,
        "skipped_dependency": skipped_dependency,
        "unresolved_dependencies": unresolved_dependencies,
        "blocked_handoffs": blocked_handoffs,
        "stale_tasks": stale_tasks,
        "failures": failures,
        "max_concurrency": max_concurrency,
    }


def finalize_objective_bundle(
    project_root: Path,
    run_id: str,
    phase: str,
    objective_id: str,
    *,
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    max_concurrency: int,
    allow_bundle_repair: bool = True,
    scheduler_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    tasks = {task["task_id"]: task for task in phase_tasks(run_dir, phase) if task["objective_id"] == objective_id}
    if objective_plan_has_no_phase_work(run_dir, phase, objective_id):
        return {
            "objective_id": objective_id,
            "status": "accepted",
            "bundle_ids": [],
            "included_tasks": [],
            "rejection_reasons": [],
        }
    bundle_specs = objective_bundle_specs(run_dir, phase, objective_id, list(tasks))
    accepted_bundles = []
    rejected_bundles = []
    blocked_bundles = []
    missing_by_bundle: dict[str, list[str]] = {}
    executed_task_ids = {
        str(item.get("task_id") or "").strip()
        for item in (scheduler_summary or {}).get("executed", [])
        if isinstance(item, dict) and str(item.get("task_id") or "").strip()
    }
    skipped_existing_task_ids = {
        str(task_id).strip()
        for task_id in (scheduler_summary or {}).get("skipped_existing", [])
        if str(task_id).strip()
    }

    for bundle_spec in bundle_specs:
        report_paths = []
        missing = []
        for task_id in bundle_spec["task_ids"]:
            report_path = run_dir / "reports" / f"{task_id}.json"
            if report_path.exists():
                report_paths.append(report_path)
            else:
                missing.append(task_id)
        if missing:
            missing_by_bundle[bundle_spec["bundle_id"]] = missing
            continue

        existing_bundle_path = run_dir / "bundles" / f"{bundle_spec['bundle_id']}.json"
        if existing_bundle_path.exists():
            existing_bundle = read_json(existing_bundle_path)
            if (
                existing_bundle.get("status") == "accepted"
                and existing_bundle.get("phase") == phase
                and existing_bundle.get("objective_id") == objective_id
                and list(existing_bundle.get("included_tasks", [])) == list(bundle_spec["task_ids"])
                and not any(task_id in executed_task_ids for task_id in bundle_spec["task_ids"])
                and all(task_id in skipped_existing_task_ids for task_id in bundle_spec["task_ids"])
            ):
                landing_results = existing_bundle.get("landing_results", [])
                isolated_task_ids = [
                    task_id
                    for task_id in bundle_spec["task_ids"]
                    if tasks.get(task_id, {}).get("execution_mode") == "isolated_write"
                ]
                landing_complete = (
                    not isolated_task_ids
                    or (
                        isinstance(landing_results, list)
                        and {item.get("task_id") for item in landing_results if isinstance(item, dict)} >= set(isolated_task_ids)
                        and all(
                            isinstance(item, dict) and item.get("status") == "merged"
                            for item in landing_results
                            if isinstance(item, dict) and item.get("task_id") in isolated_task_ids
                        )
                    )
                )
                if landing_complete:
                    accepted_bundles.append(existing_bundle)
                    record_event(
                        project_root,
                        run_id,
                        phase=phase,
                        activity_id=None,
                        event_type="bundle.reused_existing",
                        message=f"Reused accepted bundle {bundle_spec['bundle_id']} with existing landing results.",
                        payload={"bundle_id": bundle_spec["bundle_id"], "task_ids": bundle_spec["task_ids"]},
                    )
                    continue

        assemble_review_bundle(
            project_root,
            run_id,
            bundle_spec["bundle_id"],
            report_paths,
            f"objectives.{objective_id}.objective-manager",
            f"objectives.{objective_id}.acceptance-manager",
        )
        bundle = review_bundle(project_root, run_id, bundle_spec["bundle_id"])
        if bundle["status"] != "accepted":
            if allow_bundle_repair:
                repaired_summary = attempt_bundle_repair(
                    project_root,
                    run_id,
                    phase=phase,
                    objective_id=objective_id,
                    bundle=bundle,
                    sandbox_mode=sandbox_mode,
                    codex_path=codex_path,
                    timeout_seconds=timeout_seconds,
                    max_concurrency=max_concurrency,
                )
                if repaired_summary is not None:
                    return repaired_summary
            rejected_bundles.append(bundle)
            continue
        landing = land_accepted_bundle(project_root, run_id, bundle)
        if landing["status"] == "blocked":
            blocked_bundles.append(landing["bundle"])
            continue
        accepted_bundles.append(landing["bundle"])

    if missing_by_bundle:
        return {
            "objective_id": objective_id,
            "status": "pending",
            "reason": "missing_reports",
            "missing_by_bundle": missing_by_bundle,
        }
    if blocked_bundles:
        return {
            "objective_id": objective_id,
            "status": "blocked",
            "accepted_bundle_ids": [bundle["bundle_id"] for bundle in accepted_bundles],
            "blocked_bundle_ids": [bundle["bundle_id"] for bundle in blocked_bundles],
            "rejection_reasons": [reason for bundle in blocked_bundles for reason in bundle.get("rejection_reasons", [])],
        }
    if rejected_bundles:
        return {
            "objective_id": objective_id,
            "status": "rejected",
            "accepted_bundle_ids": [bundle["bundle_id"] for bundle in accepted_bundles],
            "rejection_reasons": [reason for bundle in rejected_bundles for reason in bundle.get("rejection_reasons", [])],
        }
    return {
        "objective_id": objective_id,
        "status": "accepted",
        "bundle_ids": [bundle["bundle_id"] for bundle in accepted_bundles],
        "included_tasks": [task_id for bundle in accepted_bundles for task_id in bundle["included_tasks"]],
        "rejection_reasons": [],
    }


def write_manager_summary(run_dir: Path, summary_id: str, payload: dict[str, Any]) -> None:
    manager_dir = ensure_dir(run_dir / "manager-runs")
    write_json(manager_dir / f"{summary_id}.json", payload)


def default_operator_command(run_id: str, action: str, *, phase: str | None = None) -> str:
    suffix = "--sandbox read-only --max-concurrency 2 --timeout-seconds 600 --watch"
    if action == "apply-feedback":
        return f"python3 -m company_orchestrator apply-feedback {run_id} {suffix}"
    if action == "apply-approved-changes":
        return f"python3 -m company_orchestrator apply-approved-changes {run_id} {suffix}"
    if action == "plan-phase":
        return f"python3 -m company_orchestrator plan-phase {run_id} {suffix}"
    if action == "plan-phase-replace":
        return f"python3 -m company_orchestrator plan-phase {run_id} --replace {suffix}"
    if action == "run-phase":
        return f"python3 -m company_orchestrator run-phase {run_id} {suffix}"
    if action == "resume-phase":
        return f"python3 -m company_orchestrator resume-phase {run_id} {suffix}"
    if action == "approve-phase":
        if phase is None:
            raise ValueError("phase is required for approve-phase guidance")
        return f"python3 -m company_orchestrator approve-phase {run_id} {phase}"
    if action == "advance-phase":
        return f"python3 -m company_orchestrator advance-phase {run_id}"
    raise ValueError(f"Unsupported operator action {action}")


def run_guidance(
    project_root: Path,
    run_id: str,
    *,
    phase: str | None = None,
    tasks: list[dict[str, Any]] | None = None,
    scheduler_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase_plan = read_json(run_dir / "phase-plan.json")
    active_phase_name = phase or phase_plan["current_phase"]
    phase_state = next(item for item in phase_plan["phases"] if item["phase"] == active_phase_name)
    feedback_reentry = approved_feedback_reentry_state(project_root, run_id)
    approved_feedback = list(feedback_reentry["selected_feedback"])
    if approved_feedback:
        blocking_activities = list(feedback_reentry["blocking_activities"])
        if blocking_activities:
            blocker_summary = ", ".join(
                f"{activity['activity_id']} ({activity['status']})" for activity in blocking_activities
            )
            return {
                "run_status": "working",
                "run_status_reason": (
                    "Approved user feedback cannot be applied yet because targeted objectives still have active work: "
                    + blocker_summary
                ),
                "next_action_command": None,
                "next_action_reason": "Wait for the active work to finish before applying feedback.",
                "review_doc_path": None,
                "phase_report_path": None,
                "phase_recommendation": None,
            }
        return {
            "run_status": "recoverable",
            "run_status_reason": "Approved user feedback is waiting to be applied before the run can continue.",
            "next_action_command": default_operator_command(run_id, "apply-feedback"),
            "next_action_reason": "Apply the approved user feedback to replan only the owning objectives and rerun validation.",
            "review_doc_path": None,
            "phase_report_path": None,
            "phase_recommendation": None,
        }
    if (
        phase_state.get("status") == "complete"
        and active_phase_name == phase_plan["phases"][-1]["phase"]
        and all(item.get("status") == "complete" for item in phase_plan.get("phases", []))
    ):
        phase_report_json_path = run_dir / "phase-reports" / f"{active_phase_name}.json"
        phase_report_md_path = run_dir / "phase-reports" / f"{active_phase_name}.md"
        phase_report = load_optional_json(phase_report_json_path)
        phase_report_path = (
            str(phase_report_json_path.relative_to(project_root)) if phase_report_json_path.exists() else None
        )
        review_doc_path = (
            str(phase_report_md_path.relative_to(project_root)) if phase_report_md_path.exists() else phase_report_path
        )
        return {
            "run_status": "complete",
            "run_status_reason": f"{active_phase_name.title()} is complete and the run has finished successfully.",
            "next_action_command": None,
            "next_action_reason": "No further action is required.",
            "review_doc_path": review_doc_path,
            "phase_report_path": phase_report_path,
            "phase_recommendation": phase_report.get("recommendation") if phase_report else None,
        }
    phase_tasks_payload = tasks if tasks is not None else phase_tasks(run_dir, active_phase_name)
    phase_task_ids = {task["task_id"] for task in phase_tasks_payload}
    activities = list_activities(project_root, run_id, phase=active_phase_name)
    active_activities = [
        activity
        for activity in activities
        if activity["status"]
        not in {
            "queued",
            "waiting_dependencies",
            "ready_for_bundle_review",
            "blocked",
            "needs_revision",
            "completed",
            "failed",
            "accepted",
            "rejected",
            "skipped_existing",
            "interrupted",
            "recovered",
            "abandoned",
        }
    ]
    queued_activities = [activity for activity in activities if activity["status"] == "queued"]
    blocked_activities = [
        activity for activity in activities if activity["status"] in {"waiting_dependencies", "blocked", "needs_revision"}
    ]
    interrupted_activities = [
        activity for activity in activities if activity["status"] in {"interrupted", "failed", "abandoned"}
    ]
    phase_report_json_path = run_dir / "phase-reports" / f"{active_phase_name}.json"
    phase_report_md_path = run_dir / "phase-reports" / f"{active_phase_name}.md"
    phase_report = load_optional_json(phase_report_json_path)
    phase_report_path = (
        str(phase_report_json_path.relative_to(project_root)) if phase_report_json_path.exists() else None
    )
    review_doc_path = str(phase_report_md_path.relative_to(project_root)) if phase_report_md_path.exists() else phase_report_path
    effective_scheduler_summary = scheduler_summary
    if effective_scheduler_summary is None:
        manager_summary = load_optional_json(run_dir / "manager-runs" / f"phase-{active_phase_name}.json") or {}
        effective_scheduler_summary = manager_summary.get("scheduled", {})
    next_action_command = suggested_recovery_command(
        project_root,
        run_id,
        active_phase_name,
        phase_tasks_payload,
        effective_scheduler_summary or {},
    )
    if active_phase_name == "polish" and polish_hold_is_exhausted(project_root, run_id, phase_report):
        next_action_command = None

    if active_activities:
        return {
            "run_status": "working",
            "run_status_reason": (
                f"{len(active_activities)} active activities, {len(queued_activities)} queued, "
                f"{len(blocked_activities)} blocked in {active_phase_name}."
            ),
            "next_action_command": None,
            "next_action_reason": "Monitor the run. No manual action is required while work is active.",
            "review_doc_path": review_doc_path,
            "phase_report_path": phase_report_path,
            "phase_recommendation": phase_report.get("recommendation") if phase_report else None,
        }

    if (
        queued_activities
        and next_action_command is not None
        and (
            phase_report is None
            or phase_report.get("recommendation") != "hold"
            or hold_recovery_requires_explicit_external_input(next_action_command)
        )
    ):
        return {
            "run_status": "recoverable",
            "run_status_reason": (
                f"{active_phase_name.title()} has queued work but no active workers; the run can continue automatically."
            ),
            "next_action_command": next_action_command,
            "next_action_reason": "Resume the phase to continue queued work from the current checkpoint.",
            "review_doc_path": review_doc_path,
            "phase_report_path": phase_report_path,
            "phase_recommendation": phase_report.get("recommendation") if phase_report else None,
        }

    if next_action_command is not None and "apply-approved-changes" in next_action_command:
        return {
            "run_status": "recoverable",
            "run_status_reason": "Approved change requests are waiting to be applied before the run can continue.",
            "next_action_command": next_action_command,
            "next_action_reason": "Apply approved changes to replan only the producer and impacted consumer objectives.",
            "review_doc_path": review_doc_path,
            "phase_report_path": phase_report_path,
            "phase_recommendation": phase_report.get("recommendation") if phase_report else None,
        }

    if active_phase_name == "polish" and actionable_release_repair_diagnostics(phase_report):
        if polish_release_repair_attempts(project_root, run_id) < MAX_POLISH_RELEASE_REPAIR_ATTEMPTS:
            return {
                "run_status": "recoverable",
                "run_status_reason": (
                    "Polish validation checklist failed, but the run can retry targeted owner repairs."
                ),
                "next_action_command": default_operator_command(run_id, "run-phase"),
                "next_action_reason": "Rerun polish to trigger targeted repair and re-evaluate the checklist.",
                "review_doc_path": review_doc_path,
                "phase_report_path": phase_report_path,
                "phase_recommendation": phase_report.get("recommendation") if phase_report else None,
            }

    if phase_report is not None and not phase_state.get("human_approved", False):
        recommendation = phase_report["recommendation"]
        if recommendation == "advance":
            return {
                "run_status": "ready_for_review",
                "run_status_reason": (
                    f"{active_phase_name.title()} phase report recommends advance and is waiting for human approval."
                ),
                "next_action_command": default_operator_command(run_id, "approve-phase", phase=active_phase_name),
                "next_action_reason": "Review the phase report, then record approval to unlock advancement.",
                "review_doc_path": review_doc_path,
                "phase_report_path": phase_report_path,
                "phase_recommendation": recommendation,
            }
        if hold_recovery_requires_explicit_external_input(next_action_command):
            return {
                "run_status": "recoverable",
                "run_status_reason": (
                    f"{active_phase_name.title()} phase report recommends hold, but the run has a concrete recovery path."
                ),
                "next_action_command": next_action_command,
                "next_action_reason": "Review the report if needed, then run the suggested recovery command to continue.",
                "review_doc_path": review_doc_path,
                "phase_report_path": phase_report_path,
                "phase_recommendation": recommendation,
            }
        if active_phase_name == "polish" and polish_hold_is_exhausted(project_root, run_id, phase_report):
            return {
                "run_status": "ready_for_review",
                "run_status_reason": (
                    "Polish phase report recommends hold and the repair budget is exhausted."
                ),
                "next_action_command": None,
                "next_action_reason": "Review the phase report and provide new inputs or explicit approval before continuing.",
                "review_doc_path": review_doc_path,
                "phase_report_path": phase_report_path,
                "phase_recommendation": recommendation,
            }
        return {
            "run_status": "ready_for_review",
            "run_status_reason": (
                f"{active_phase_name.title()} phase report recommends hold and needs human review before continuing."
            ),
            "next_action_command": None,
            "next_action_reason": "Review the phase report and unresolved risks before choosing a recovery command.",
            "review_doc_path": review_doc_path,
            "phase_report_path": phase_report_path,
            "phase_recommendation": recommendation,
        }

    if (
        phase_report is not None
        and phase_report.get("recommendation") == "advance"
        and phase_state.get("human_approved", False)
        and phase_state.get("status") != "complete"
    ):
        return {
            "run_status": "ready_to_advance",
            "run_status_reason": f"{active_phase_name.title()} is approved and ready to advance to the next phase.",
            "next_action_command": default_operator_command(run_id, "advance-phase"),
            "next_action_reason": "Advance the run to unlock planning and execution for the next phase.",
            "review_doc_path": review_doc_path,
            "phase_report_path": phase_report_path,
            "phase_recommendation": phase_report.get("recommendation"),
        }

    if next_action_command is not None:
        unresolved = (effective_scheduler_summary or {}).get("unresolved_dependencies", {})
        task_handoff_ids = {
            handoff["handoff_id"]
            for handoff in list_handoffs(project_root / "runs" / run_id, phase=active_phase_name)
        }
        has_non_runtime_blockers = any(
            blocker not in phase_task_ids and blocker not in task_handoff_ids
            for blockers in unresolved.values()
            for blocker in blockers
        )
        if has_non_runtime_blockers:
            reason = "Planned work is blocked by stale planning dependencies and should be replanned."
        elif interrupted_activities:
            reason = "The run has interrupted work that can be recovered automatically."
        else:
            reason = "The run has blocked work that can be resumed safely."
        return {
            "run_status": "recoverable",
            "run_status_reason": reason,
            "next_action_command": next_action_command,
            "next_action_reason": "Run the suggested recovery command to continue from the current checkpoint.",
            "review_doc_path": review_doc_path,
            "phase_report_path": phase_report_path,
            "phase_recommendation": phase_report.get("recommendation") if phase_report else None,
        }

    if phase_tasks_payload:
        return {
            "run_status": "working",
            "run_status_reason": f"{active_phase_name.title()} has planned tasks and is ready to execute.",
            "next_action_command": default_operator_command(run_id, "run-phase"),
            "next_action_reason": "Execute the current phase to start worker activity.",
            "review_doc_path": review_doc_path,
            "phase_report_path": phase_report_path,
            "phase_recommendation": phase_report.get("recommendation") if phase_report else None,
        }

    return {
        "run_status": "working",
        "run_status_reason": f"{active_phase_name.title()} is active and ready for planning.",
        "next_action_command": default_operator_command(run_id, "plan-phase"),
        "next_action_reason": "Plan the current phase to materialize objective and task work.",
        "review_doc_path": review_doc_path,
        "phase_report_path": phase_report_path,
        "phase_recommendation": phase_report.get("recommendation") if phase_report else None,
    }


def suggested_recovery_command(
    project_root: Path,
    run_id: str,
    phase: str,
    tasks: list[dict[str, Any]],
    scheduler_summary: dict[str, Any],
) -> str | None:
    phase_report = load_optional_json(project_root / "runs" / run_id / "phase-reports" / f"{phase}.json")
    if phase == "polish" and polish_hold_is_exhausted(project_root, run_id, phase_report):
        return None
    if active_approved_feedback(project_root, run_id):
        return default_operator_command(run_id, "apply-feedback")
    if active_approved_change_requests(project_root, run_id):
        return default_operator_command(run_id, "apply-approved-changes")
    task_ids = {task["task_id"] for task in tasks}
    handoff_ids = {handoff["handoff_id"] for handoff in list_handoffs(project_root / "runs" / run_id, phase=phase)}
    unresolved = scheduler_summary.get("unresolved_dependencies", {})
    skipped_dependency = scheduler_summary.get("skipped_dependency", {})
    failures = scheduler_summary.get("failures") or []
    blocked_handoffs = scheduler_summary.get("blocked_handoffs") or {}
    activities = list_activities(project_root, run_id, phase=phase)
    interrupted_planning = any(
        activity["kind"] in {"objective_plan", "capability_plan"} and activity["status"] in {"interrupted", "failed"}
        for activity in activities
    )
    blocked_or_interrupted_tasks = any(
        activity["kind"] == "task_execution" and activity["status"] in {"waiting_dependencies", "blocked", "needs_revision", "interrupted", "failed"}
        for activity in activities
    )
    has_non_runtime_blockers = any(
        blocker not in task_ids and blocker not in handoff_ids
        for blockers in unresolved.values()
        for blocker in blockers
    )
    if has_non_runtime_blockers:
        return default_operator_command(run_id, "plan-phase-replace")
    if interrupted_planning and not blocked_or_interrupted_tasks:
        return default_operator_command(run_id, "plan-phase-replace")
    if unresolved or skipped_dependency or blocked_handoffs or failures or blocked_or_interrupted_tasks:
        return default_operator_command(run_id, "resume-phase")
    return None
