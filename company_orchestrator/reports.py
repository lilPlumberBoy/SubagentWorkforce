from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .bundle_plans import objective_bundle_specs, objective_plan_has_no_phase_work
from .constants import PHASES
from .filesystem import read_json, write_json, write_text
from .handoffs import HANDOFF_BLOCKED, HANDOFF_SATISFIED, PENDING_HANDOFF_STATUSES
from .live import initialize_live_run, record_event, refresh_run_state
from .observability import summarize_observability_for_phase
from .parallelism import summarize_parallelism_for_phase
from .recovery import summarize_recovery_for_phase
from .schemas import validate_document
from .worktree_manager import integration_workspace_path
from .worktree_manager import WorktreeError, cleanup_phase_task_worktrees

BACKEND_RELEASE_FAILURE_MARKERS = (
    "crud-contract",
    "validation-errors",
    "durability",
    "post /api/todos",
    "get /api/todos",
    "patch /api/todos",
    "delete /api/todos",
    "repository ",
)
FRONTEND_RELEASE_FAILURE_MARKERS = (
    "apps/todo/frontend/test/",
    "todo app ",
    "todo api client",
    "frontend review evidence",
)
MIDDLEWARE_RELEASE_FAILURE_MARKERS = (
    "apps/todo/runtime/test/",
    "runtime connectivity",
    "e2e smoke",
    "runtime startup",
    "review evidence package",
    "todo runtime",
)
REPO_PATH_PATTERN = re.compile(r"(apps/todo/[^\s'\"`:),]+|package\.json)")


def generate_phase_report(project_root: Path, run_id: str) -> tuple[dict[str, Any], Path]:
    run_dir = project_root / "runs" / run_id
    initialize_live_run(project_root, run_id)
    phase_plan = read_json(run_dir / "phase-plan.json")
    objective_map = read_json(run_dir / "objective-map.json")
    phase = phase_plan["current_phase"]
    release_validation_summary = (
        evaluate_polish_release_validation(project_root, run_id)
        if phase == "polish"
        else None
    )
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
            no_phase_work = objective_plan_has_no_phase_work(run_dir, phase, objective["objective_id"])
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
            elif no_phase_work:
                status = "accepted"
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

    if phase == "polish" and release_validation_summary is not None:
        release_gate_passed = release_validation_summary["status"] == "passed"
        if release_gate_passed:
            outcomes = [
                {
                    **item,
                    "status": "accepted",
                }
                for item in outcomes
            ]
        recommendation = "advance" if release_gate_passed else "hold"
    else:
        recommendation = "advance" if outcomes and all(item["status"] == "accepted" for item in outcomes) else "hold"
    accepted_bundle_ids = [bundle_id for item in outcomes for bundle_id in item["accepted_bundles"]]
    phase_tasks = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        task = read_json(path)
        if task["phase"] == phase:
            phase_tasks.append(task)
    parallelism_summary = summarize_parallelism_for_phase(run_dir, phase, phase_tasks)
    collaboration_summary = summarize_collaboration_for_phase(run_dir, phase, objective_map["objectives"])
    observability_summary = summarize_observability_for_phase(project_root, run_id, phase)
    recovery_summary = summarize_recovery_for_phase(project_root, run_id, phase)
    unresolved_risks = [bundle["bundle_id"] for bundle in rejected] + [bundle["bundle_id"] for bundle in blocked]
    if release_validation_summary is not None and release_validation_summary["status"] != "passed":
        unresolved_risks.append("polish_release_validation")
    payload = {
        "schema": "phase-report.v1",
        "run_id": run_id,
        "phase": phase,
        "summary": f"{phase} phase report for {run_id}",
        "objective_outcomes": outcomes,
        "accepted_bundles": accepted_bundle_ids,
        "unresolved_risks": unresolved_risks,
        "parallelism_summary": parallelism_summary,
        "collaboration_summary": collaboration_summary,
        "observability_summary": observability_summary,
        "recovery_summary": recovery_summary,
        "release_validation_summary": release_validation_summary,
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
            "## Release Validation",
        ]
    )
    release_validation = report.get("release_validation_summary")
    if isinstance(release_validation, dict):
        lines.extend(
            [
                f"- status: {release_validation.get('status', 'unknown')}",
                f"- command: {release_validation.get('command', 'none')}",
                f"- working directory: {release_validation.get('working_directory', 'none')}",
                f"- report: {release_validation.get('report_path', 'none')}",
                f"- summary: {release_validation.get('summary', 'none')}",
            ]
        )
    else:
        lines.append("- none")
    lines.extend(
        [
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
    lines.extend(
        [
            "",
            "## Observability Summary",
            f"- total calls: {report['observability_summary']['total_calls']}",
            f"- completed calls: {report['observability_summary']['completed_calls']}",
            f"- failed calls: {report['observability_summary']['failed_calls']}",
            f"- timed out calls: {report['observability_summary']['timed_out_calls']}",
            f"- retry scheduled calls: {report['observability_summary']['retry_scheduled_calls']}",
            f"- input tokens: {report['observability_summary']['total_input_tokens']}",
            f"- cached input tokens: {report['observability_summary']['total_cached_input_tokens']}",
            f"- output tokens: {report['observability_summary']['total_output_tokens']}",
            f"- prompt chars: {report['observability_summary']['total_prompt_chars']}",
            f"- prompt lines: {report['observability_summary']['total_prompt_lines']}",
            f"- average latency: {report['observability_summary']['average_latency_ms']}ms",
            f"- max latency: {report['observability_summary']['max_latency_ms']}ms",
            f"- average queue wait: {report['observability_summary']['average_queue_wait_ms']}ms",
            "",
            "## Recovery Summary",
            f"- interrupted activities: {report['recovery_summary']['interrupted_activities']}",
            f"- recovered activities: {report['recovery_summary']['recovered_activities']}",
            f"- abandoned attempts: {report['recovery_summary']['abandoned_attempts']}",
        ]
    )
    lines.extend(
        [
            "",
            "## Collaboration Summary",
            f"- total handoffs: {report['collaboration_summary']['total_handoffs']}",
            f"- blocking handoffs: {report['collaboration_summary']['blocking_handoffs']}",
            f"- satisfied handoffs: {report['collaboration_summary']['satisfied_handoffs']}",
            f"- pending handoffs: {report['collaboration_summary']['pending_handoffs']}",
            f"- blocked handoffs: {report['collaboration_summary']['blocked_handoffs']}",
        ]
    )
    if report["collaboration_summary"]["handoffs_by_objective"]:
        for item in report["collaboration_summary"]["handoffs_by_objective"]:
            handoffs = ", ".join(item["handoff_ids"]) or "none"
            lines.append(
                f"- {item['objective_id']}: {item['total_handoffs']} handoffs "
                f"({item['blocking_handoffs']} blocking, {item['satisfied_handoffs']} satisfied, "
                f"{item['pending_handoffs']} pending, {item['blocked_handoffs']} blocked) [{handoffs}]"
            )
    else:
        lines.append("- by objective: none")
    if report["collaboration_summary"]["incidents"]:
        lines.append("- incidents:")
        for incident in report["collaboration_summary"]["incidents"]:
            consumers = ", ".join(incident.get("consumer_task_ids", [])) or "none"
            lines.append(
                f"  - {incident['handoff_id']}: {incident['status']} ({incident['reason']}) "
                f"[consumers: {consumers}] ({incident['artifact_path']})"
            )
    else:
        lines.append("- incidents: none")
    if report["recovery_summary"]["incidents"]:
        for incident in report["recovery_summary"]["incidents"]:
            lines.append(f"- {incident['activity_id']}: {incident['status']} ({incident['reason']})")
    else:
        lines.append("- incidents: none")
    return "\n".join(lines)


def evaluate_polish_release_validation(project_root: Path, run_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase_reports_dir = run_dir / "phase-reports"
    phase_reports_dir.mkdir(parents=True, exist_ok=True)
    integration_workspace = integration_workspace_path(project_root, run_id)
    report_path = phase_reports_dir / "polish-release-validation.json"
    stdout_path = phase_reports_dir / "polish-release-validation.stdout.log"
    stderr_path = phase_reports_dir / "polish-release-validation.stderr.log"
    summary: dict[str, Any] = {
        "status": "failed",
        "command": None,
        "working_directory": str(integration_workspace.relative_to(project_root)),
        "report_path": str(report_path.relative_to(project_root)),
        "stdout_path": str(stdout_path.relative_to(project_root)),
        "stderr_path": str(stderr_path.relative_to(project_root)),
        "summary": "",
        "failure_diagnostics": [],
    }

    if not integration_workspace.exists():
        summary["summary"] = "Integration workspace is missing, so integrated polish validation could not run."
        write_json(report_path, summary)
        return summary

    package_path = integration_workspace / "package.json"
    if not package_path.exists():
        summary["summary"] = "Integration workspace package.json is missing, so no integrated release-readiness command is available."
        write_json(report_path, summary)
        return summary

    script_name = resolve_release_validation_script(package_path)
    if not script_name:
        summary["summary"] = "No release-readiness npm script is declared in the integration workspace package.json."
        write_json(report_path, summary)
        return summary

    command = ["npm", "run", script_name]
    summary["command"] = " ".join(command)
    record_event(
        project_root,
        run_id,
        phase="polish",
        activity_id=None,
        event_type="phase.release_validation_started",
        message=f"Running polish release validation via {summary['command']}.",
        payload={"working_directory": summary["working_directory"], "command": summary["command"]},
    )
    try:
        completed = subprocess.run(
            command,
            cwd=integration_workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "CI": "1"},
            timeout=900,
        )
        if completed.returncode == 0:
            summary["status"] = "passed"
            summary["summary"] = "Integrated release-readiness validation passed."
        else:
            summary["status"] = "failed"
            summary["summary"] = compact_release_validation_failure(completed.stdout, completed.stderr)
            summary["failure_diagnostics"] = extract_release_validation_diagnostics(
                project_root,
                run_id,
                completed.stdout,
                completed.stderr,
            )
        write_text(stdout_path, completed.stdout)
        write_text(stderr_path, completed.stderr)
    except subprocess.TimeoutExpired as exc:
        stdout_text = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr_text = exc.stderr if isinstance(exc.stderr, str) else ""
        write_text(stdout_path, stdout_text)
        write_text(stderr_path, stderr_text)
        summary["status"] = "failed"
        summary["summary"] = "Integrated release-readiness validation timed out after 900 seconds."
        summary["failure_diagnostics"] = extract_release_validation_diagnostics(
            project_root,
            run_id,
            stdout_text,
            stderr_text,
        )
    write_json(report_path, summary)
    record_event(
        project_root,
        run_id,
        phase="polish",
        activity_id=None,
        event_type="phase.release_validation_completed",
        message=summary["summary"],
        payload={
            "status": summary["status"],
            "command": summary["command"],
            "report_path": summary["report_path"],
            "failure_diagnostics": len(summary.get("failure_diagnostics") or []),
        },
    )
    return summary


def resolve_release_validation_script(package_path: Path) -> str | None:
    try:
        package_payload = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = package_payload.get("scripts")
    if not isinstance(scripts, dict):
        return None
    if "validate:release-readiness" in scripts:
        return "validate:release-readiness"
    if "validate:todo-release-readiness" in scripts:
        return "validate:todo-release-readiness"
    for script_name in sorted(scripts):
        if script_name.startswith("validate:") and script_name.endswith("release-readiness"):
            return script_name
    return None


def compact_release_validation_failure(stdout: str, stderr: str) -> str:
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    if not combined:
        return "Integrated release-readiness validation failed without stdout or stderr output."
    lines = [line.strip() for line in combined.splitlines() if line.strip()]
    if not lines:
        return "Integrated release-readiness validation failed without a readable error summary."
    preview = " ".join(lines[-6:])
    return preview[:397] + "..." if len(preview) > 400 else preview


def extract_release_validation_diagnostics(
    project_root: Path,
    run_id: str,
    stdout: str,
    stderr: str,
) -> list[dict[str, Any]]:
    blocks = release_validation_failure_blocks(stdout, stderr)
    objective_map_path = project_root / "runs" / run_id / "objective-map.json"
    objective_map = read_json(objective_map_path) if objective_map_path.exists() else {"objectives": []}
    owner_by_capability = release_failure_owner_objectives(objective_map)
    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for block in blocks:
        paths = extract_release_failure_paths(block)
        owner_capability = classify_release_failure_capability(block, paths)
        category = classify_release_failure_category(block, owner_capability)
        source_test = release_failure_source_test(block)
        owner_objective_id = owner_by_capability.get(owner_capability) if owner_capability else None
        excerpt = compact_text_block(block, max_lines=8, max_chars=420)
        diagnostic = {
            "category": category,
            "owner_capability": owner_capability,
            "owner_objective_id": owner_objective_id,
            "source_test": source_test,
            "paths": paths,
            "excerpt": excerpt,
            "repairable": owner_objective_id is not None,
        }
        key = (
            diagnostic["category"],
            diagnostic["owner_capability"],
            tuple(diagnostic["paths"]),
            diagnostic["excerpt"],
        )
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(diagnostic)
    return diagnostics


def release_validation_failure_blocks(stdout: str, stderr: str) -> list[str]:
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    if not combined:
        return []
    lines = combined.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    collecting = False
    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("✖ "):
            if current:
                blocks.append(current)
            current = [stripped]
            collecting = True
            continue
        if not collecting:
            continue
        if stripped.startswith(("✔ ", "ℹ ", "> ")) and current:
            blocks.append(current)
            current = []
            collecting = False
            continue
        if not stripped and current:
            blocks.append(current)
            current = []
            collecting = False
            continue
        current.append(stripped or line)
    if current:
        blocks.append(current)
    if not blocks and combined:
        return [combined]
    return ["\n".join(line for line in block if line) for block in blocks]


def release_failure_owner_objectives(objective_map: dict[str, Any]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for objective in objective_map.get("objectives", []):
        objective_id = str(objective.get("objective_id", "")).strip()
        if not objective_id:
            continue
        for capability in objective.get("capabilities", []):
            capability_name = str(capability).strip()
            if capability_name and capability_name not in owners:
                owners[capability_name] = objective_id
    return owners


def extract_release_failure_paths(block: str) -> list[str]:
    paths: list[str] = []
    for match in REPO_PATH_PATTERN.findall(block):
        normalized = str(match).strip().rstrip(".,:;)]")
        if normalized and normalized not in paths:
            paths.append(normalized)
    return paths


def classify_release_failure_capability(block: str, paths: list[str]) -> str | None:
    lower = block.lower()
    if any(path.startswith("apps/todo/frontend/") for path in paths):
        return "frontend"
    if any(path.startswith("apps/todo/backend/") for path in paths):
        return "backend"
    if any(path.startswith("apps/todo/runtime/") for path in paths):
        return "middleware"
    if any(marker in lower for marker in FRONTEND_RELEASE_FAILURE_MARKERS):
        return "frontend"
    if any(marker in lower for marker in BACKEND_RELEASE_FAILURE_MARKERS):
        return "backend"
    if any(marker in lower for marker in MIDDLEWARE_RELEASE_FAILURE_MARKERS):
        return "middleware"
    return None


def classify_release_failure_category(block: str, owner_capability: str | None) -> str:
    lower = block.lower()
    if "err_module_not_found" in lower or "cannot find module" in lower:
        return "module_resolution"
    if "notfounderror" in lower or "route not found" in lower:
        return "runtime_asset_delivery"
    if owner_capability == "backend":
        return "backend_contract"
    if owner_capability == "frontend":
        return "frontend_validation"
    if owner_capability == "middleware":
        return "runtime_integration"
    return "release_validation_failure"


def release_failure_source_test(block: str) -> str | None:
    first_line = next((line.strip() for line in block.splitlines() if line.strip()), "")
    if not first_line:
        return None
    if first_line.startswith("✖ "):
        return first_line[2:].strip()
    return first_line


def compact_text_block(block: str, *, max_lines: int, max_chars: int) -> str:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    text = " ".join(lines[:max_lines])
    return text[: max_chars - 3] + "..." if len(text) > max_chars else text


def summarize_collaboration_for_phase(run_dir: Path, phase: str, objectives: list[dict[str, Any]]) -> dict[str, Any]:
    collaboration_dir = run_dir / "collaboration-plans"
    handoffs = []
    for path in sorted(collaboration_dir.glob("*.json")):
        payload = read_json(path)
        if payload["phase"] == phase:
            handoffs.append(payload)
    by_objective = []
    incidents = []
    for objective in objectives:
        objective_handoffs = [handoff for handoff in handoffs if handoff["objective_id"] == objective["objective_id"]]
        satisfied_handoffs = sum(1 for handoff in objective_handoffs if handoff.get("status") == HANDOFF_SATISFIED)
        pending_handoffs = sum(1 for handoff in objective_handoffs if handoff.get("status") in PENDING_HANDOFF_STATUSES)
        blocked_handoffs = sum(1 for handoff in objective_handoffs if handoff.get("status") == HANDOFF_BLOCKED)
        by_objective.append(
            {
                "objective_id": objective["objective_id"],
                "total_handoffs": len(objective_handoffs),
                "blocking_handoffs": sum(1 for handoff in objective_handoffs if handoff["blocking"]),
                "satisfied_handoffs": satisfied_handoffs,
                "pending_handoffs": pending_handoffs,
                "blocked_handoffs": blocked_handoffs,
                "handoff_ids": [handoff["handoff_id"] for handoff in objective_handoffs],
            }
        )
        for handoff in objective_handoffs:
            status = handoff.get("status")
            if status in {HANDOFF_SATISFIED, "planned"}:
                continue
            incidents.append(
                {
                    "handoff_id": handoff["handoff_id"],
                    "status": status,
                    "reason": handoff.get("status_reason") or "Handoff still waiting on required collaboration.",
                    "artifact_path": str((run_dir / "collaboration-plans" / f"{handoff['handoff_id']}.json").relative_to(run_dir.parent.parent)),
                    "objective_id": handoff["objective_id"],
                    "consumer_task_ids": list(handoff.get("to_task_ids", [])),
                }
            )
    return {
        "total_handoffs": len(handoffs),
        "blocking_handoffs": sum(1 for handoff in handoffs if handoff["blocking"]),
        "satisfied_handoffs": sum(1 for handoff in handoffs if handoff.get("status") == HANDOFF_SATISFIED),
        "pending_handoffs": sum(1 for handoff in handoffs if handoff.get("status") in PENDING_HANDOFF_STATUSES),
        "blocked_handoffs": sum(1 for handoff in handoffs if handoff.get("status") == HANDOFF_BLOCKED),
        "handoffs_by_objective": by_objective,
        "incidents": incidents,
    }


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
