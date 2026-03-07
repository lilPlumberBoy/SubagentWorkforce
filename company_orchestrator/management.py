from __future__ import annotations

from pathlib import Path
from typing import Any

from .bundle_plans import objective_bundle_specs
from .bundles import assemble_review_bundle, review_bundle
from .executor import ExecutorError, execute_task
from .filesystem import ensure_dir, read_json, write_json
from .reports import generate_phase_report


def run_phase(
    project_root: Path,
    run_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    force: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase = active_phase(run_dir)
    tasks = phase_tasks(run_dir, phase)
    objective_ids = sorted({task["objective_id"] for task in tasks})
    scheduler_summary = schedule_tasks(
        project_root,
        run_id,
        tasks,
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        force=force,
        timeout_seconds=timeout_seconds,
    )
    objective_summaries = {}
    for objective_id in objective_ids:
        objective_summaries[objective_id] = finalize_objective_bundle(project_root, run_id, phase, objective_id)
    phase_report, phase_report_path = generate_phase_report(project_root, run_id)
    summary = {
        "run_id": run_id,
        "phase": phase,
        "scheduled": scheduler_summary,
        "objectives": objective_summaries,
        "phase_report_path": str(phase_report_path.relative_to(project_root)),
        "recommendation": phase_report["recommendation"],
    }
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
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase = active_phase(run_dir)
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
    )
    objective_summary = finalize_objective_bundle(project_root, run_id, phase, objective_id)
    summary = {
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "scheduled": scheduler_summary,
        "objective": objective_summary,
    }
    write_manager_summary(run_dir, f"{phase}-{objective_id}", summary)
    return summary


def active_phase(run_dir: Path) -> str:
    phase_plan = read_json(run_dir / "phase-plan.json")
    return phase_plan["current_phase"]


def phase_tasks(run_dir: Path, phase: str) -> list[dict[str, Any]]:
    tasks = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        task = read_json(path)
        if task["phase"] == phase:
            tasks.append(task)
    return tasks


def schedule_tasks(
    project_root: Path,
    run_id: str,
    tasks: list[dict[str, Any]],
    *,
    sandbox_mode: str,
    codex_path: str,
    force: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    reports_dir = run_dir / "reports"
    tasks_by_id = {task["task_id"]: task for task in tasks}
    all_reports = {path.stem: read_json(path) for path in sorted(reports_dir.glob("*.json"))}
    existing_reports = {task_id: report for task_id, report in all_reports.items() if task_id in tasks_by_id}
    if force:
        completed: set[str] = set()
        failed: set[str] = set()
    else:
        completed = {task_id for task_id, report in all_reports.items() if report["status"] == "ready_for_bundle_review"}
        failed = {task_id for task_id, report in all_reports.items() if report["status"] != "ready_for_bundle_review"}
    pending = dict(tasks_by_id)
    executed: list[dict[str, Any]] = []
    skipped_existing: list[str] = []
    skipped_dependency: dict[str, list[str]] = {}
    unresolved_dependencies: dict[str, list[str]] = {}
    failures: list[dict[str, str]] = []

    if not force:
        for task_id in list(pending):
            if task_id in completed:
                skipped_existing.append(task_id)
                pending.pop(task_id)

    while pending:
        ready: list[dict[str, Any]] = []
        for task_id, task in list(pending.items()):
            failed_deps = [dependency for dependency in task["depends_on"] if dependency in failed or dependency in skipped_dependency]
            if failed_deps:
                skipped_dependency[task_id] = failed_deps
                pending.pop(task_id)
                continue
            unmet_deps = [dependency for dependency in task["depends_on"] if dependency not in completed]
            if not unmet_deps:
                ready.append(task)

        if not ready:
            for task_id, task in pending.items():
                unresolved_dependencies[task_id] = [
                    dependency for dependency in task["depends_on"] if dependency not in completed
                ]
            break

        for task in sorted(ready, key=lambda item: item["task_id"]):
            task_id = task["task_id"]
            pending.pop(task_id, None)
            try:
                execution_summary = execute_task(
                project_root,
                run_id,
                task_id,
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                timeout_seconds=timeout_seconds,
            )
            except ExecutorError as exc:
                failures.append({"task_id": task_id, "message": str(exc)})
                failed.add(task_id)
                continue
            executed.append(execution_summary)
            if execution_summary["status"] == "ready_for_bundle_review":
                completed.add(task_id)
            else:
                failed.add(task_id)

    return {
        "phase": active_phase(run_dir),
        "executed": executed,
        "skipped_existing": skipped_existing,
        "skipped_dependency": skipped_dependency,
        "unresolved_dependencies": unresolved_dependencies,
        "failures": failures,
    }


def finalize_objective_bundle(project_root: Path, run_id: str, phase: str, objective_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    tasks = {task["task_id"]: task for task in phase_tasks(run_dir, phase) if task["objective_id"] == objective_id}
    bundle_specs = objective_bundle_specs(run_dir, phase, objective_id, list(tasks))
    accepted_bundles = []
    rejected_bundles = []
    missing_by_bundle: dict[str, list[str]] = {}

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

        assemble_review_bundle(
            project_root,
            run_id,
            bundle_spec["bundle_id"],
            report_paths,
            f"objectives.{objective_id}.objective-manager",
            f"objectives.{objective_id}.acceptance-manager",
        )
        bundle = review_bundle(project_root, run_id, bundle_spec["bundle_id"])
        if bundle["status"] == "accepted":
            accepted_bundles.append(bundle)
        else:
            rejected_bundles.append(bundle)

    if missing_by_bundle:
        return {
            "objective_id": objective_id,
            "status": "pending",
            "reason": "missing_reports",
            "missing_by_bundle": missing_by_bundle,
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
