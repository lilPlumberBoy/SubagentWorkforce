from __future__ import annotations

from pathlib import Path
from typing import Any

from .bundle_plans import objective_bundle_specs
from .constants import PHASES
from .filesystem import read_json, write_json, write_text
from .live import initialize_live_run, record_event, refresh_run_state
from .parallelism import summarize_parallelism_for_phase
from .schemas import validate_document
from .worktree_manager import WorktreeError, cleanup_phase_task_worktrees


def generate_phase_report(project_root: Path, run_id: str) -> tuple[dict[str, Any], Path]:
    run_dir = project_root / "runs" / run_id
    initialize_live_run(project_root, run_id)
    phase_plan = read_json(run_dir / "phase-plan.json")
    objective_map = read_json(run_dir / "objective-map.json")
    phase = phase_plan["current_phase"]
    bundles = [read_json(path) for path in sorted((run_dir / "bundles").glob("*.json"))]
    accepted_by_objective = {}
    rejected = []
    blocked = []
    for bundle in bundles:
        if bundle["phase"] != phase:
            continue
        if bundle["status"] == "accepted":
            accepted_by_objective.setdefault(bundle["objective_id"], []).append(bundle["bundle_id"])
        elif bundle["status"] == "blocked":
            blocked.append(bundle)
        elif bundle["status"] == "rejected":
            rejected.append(bundle)

    outcomes = []
    for objective in objective_map["objectives"]:
        accepted_bundle_ids = accepted_by_objective.get(objective["objective_id"], [])
        manager_plan_path = run_dir / "manager-plans" / f"{phase}-{objective['objective_id']}.json"
        if manager_plan_path.exists():
            required_bundle_specs = objective_bundle_specs(run_dir, phase, objective["objective_id"], [])
            required_bundle_ids = [bundle["bundle_id"] for bundle in required_bundle_specs]
            matched_bundle_ids = [bundle_id for bundle_id in required_bundle_ids if bundle_id in accepted_bundle_ids]
            blocked_bundle_ids = {
                bundle["bundle_id"]
                for bundle in blocked
                if bundle["objective_id"] == objective["objective_id"]
            }
            if blocked_bundle_ids:
                status = "blocked"
            elif required_bundle_ids and set(required_bundle_ids).issubset(set(accepted_bundle_ids)):
                status = "accepted"
            else:
                status = "pending"
        else:
            matched_bundle_ids = accepted_bundle_ids
            if any(bundle["objective_id"] == objective["objective_id"] for bundle in blocked):
                status = "blocked"
            else:
                status = "accepted" if matched_bundle_ids else "pending"
        outcomes.append(
            {
                "objective_id": objective["objective_id"],
                "status": status,
                "accepted_bundles": matched_bundle_ids,
            }
        )

    recommendation = "advance" if outcomes and all(item["status"] == "accepted" for item in outcomes) else "hold"
    accepted_bundle_ids = [bundle_id for item in outcomes for bundle_id in item["accepted_bundles"]]
    phase_tasks = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        task = read_json(path)
        if task["phase"] == phase:
            phase_tasks.append(task)
    parallelism_summary = summarize_parallelism_for_phase(run_dir, phase, phase_tasks)
    payload = {
        "schema": "phase-report.v1",
        "run_id": run_id,
        "phase": phase,
        "summary": f"{phase} phase report for {run_id}",
        "objective_outcomes": outcomes,
        "accepted_bundles": accepted_bundle_ids,
        "unresolved_risks": [bundle["bundle_id"] for bundle in rejected] + [bundle["bundle_id"] for bundle in blocked],
        "parallelism_summary": parallelism_summary,
        "proposed_role_changes": [],
        "recommendation": recommendation,
        "human_approved": False,
    }
    validate_document(payload, "phase-report.v1", project_root)
    json_path = run_dir / "phase-reports" / f"{phase}.json"
    md_path = run_dir / "phase-reports" / f"{phase}.md"
    write_json(json_path, payload)
    write_text(md_path, render_phase_report_markdown(payload))
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=None,
        event_type="phase.report_written",
        message=f"Wrote {phase} phase report.",
        payload={"report_path": str(json_path.relative_to(project_root))},
    )
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=None,
        event_type="phase.recommendation_updated",
        message=f"{phase} phase recommendation is {recommendation}.",
        payload={"recommendation": recommendation},
    )
    return payload, json_path


def render_phase_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['phase'].title()} Phase Report",
        "",
        f"Summary: {report['summary']}",
        "",
        "## Objective Outcomes",
    ]
    for item in report["objective_outcomes"]:
        bundles = ", ".join(item["accepted_bundles"]) or "none"
        lines.append(f"- {item['objective_id']}: {item['status']} (bundles: {bundles})")
    lines.extend(
        [
            "",
            "## Recommendation",
            f"- {report['recommendation']}",
            "",
            "## Unresolved Risks",
        ]
    )
    if report["unresolved_risks"]:
        for risk in report["unresolved_risks"]:
            lines.append(f"- {risk}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Parallelism Summary",
            f"- total tasks considered: {report['parallelism_summary']['total_tasks_considered']}",
            f"- tasks run in parallel: {report['parallelism_summary']['tasks_run_in_parallel']}",
            f"- tasks serialized by policy: {report['parallelism_summary']['tasks_serialized_by_policy']}",
            f"- tasks serialized by runtime conflict: {report['parallelism_summary']['tasks_serialized_by_runtime_conflict']}",
        ]
    )
    if report["parallelism_summary"]["incidents"]:
        lines.append("- incidents:")
        for incident in report["parallelism_summary"]["incidents"]:
            lines.append(f"  - {incident['task_id']}: {incident['reason']} ({incident['artifact_path']})")
    else:
        lines.append("- incidents: none")
    return "\n".join(lines)


def record_human_approval(project_root: Path, run_id: str, phase: str, approved: bool) -> dict[str, Any]:
    phase_plan_path = project_root / "runs" / run_id / "phase-plan.json"
    phase_plan = read_json(phase_plan_path)
    for item in phase_plan["phases"]:
        if item["phase"] == phase:
            item["human_approved"] = approved
            break
    write_json(phase_plan_path, phase_plan)

    report_path = project_root / "runs" / run_id / "phase-reports" / f"{phase}.json"
    if report_path.exists():
        report = read_json(report_path)
        report["human_approved"] = approved
        write_json(report_path, report)
    if approved:
        try:
            phase_task_ids = [
                path.stem
                for path in sorted((project_root / "runs" / run_id / "tasks").glob("*.json"))
                if read_json(path)["phase"] == phase
            ]
            cleanup_phase_task_worktrees(project_root, run_id, phase_task_ids)
        except WorktreeError:
            pass
    refresh_run_state(project_root, run_id)
    return phase_plan


def advance_phase(project_root: Path, run_id: str) -> dict[str, Any]:
    phase_plan_path = project_root / "runs" / run_id / "phase-plan.json"
    phase_plan = read_json(phase_plan_path)
    current_phase = phase_plan["current_phase"]
    report_path = project_root / "runs" / run_id / "phase-reports" / f"{current_phase}.json"
    if not report_path.exists():
        raise ValueError(f"No phase report exists for {current_phase}")
    report = read_json(report_path)
    if report["recommendation"] != "advance":
        raise ValueError(f"Phase {current_phase} is not ready to advance")
    phase_state = next(item for item in phase_plan["phases"] if item["phase"] == current_phase)
    if not phase_state.get("human_approved"):
        raise ValueError(f"Phase {current_phase} requires human approval")

    current_index = PHASES.index(current_phase)
    if current_index == len(PHASES) - 1:
        phase_state["status"] = "complete"
        write_json(phase_plan_path, phase_plan)
        refresh_run_state(project_root, run_id)
        return phase_plan

    phase_state["status"] = "complete"
    next_phase = PHASES[current_index + 1]
    phase_plan["current_phase"] = next_phase
    for item in phase_plan["phases"]:
        if item["phase"] == next_phase:
            item["status"] = "active"
    write_json(phase_plan_path, phase_plan)
    refresh_run_state(project_root, run_id)
    return phase_plan
