from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .bundle_plans import objective_bundle_specs, objective_plan_has_no_phase_work
from .bundles import active_bundle_ids_for_phase
from .constants import PHASES
from .filesystem import append_text, clear_text, load_optional_json, read_json, write_json, write_text
from .handoffs import HANDOFF_BLOCKED, HANDOFF_SATISFIED, PENDING_HANDOFF_STATUSES
from .live import activity_path, initialize_live_run, record_event, refresh_run_state
from .observability import summarize_observability_for_phase
from .parallelism import summarize_parallelism_for_phase
from .recovery import summarize_recovery_for_phase
from .schemas import validate_document
from .task_graph import active_phase_tasks
from .worktree_manager import WorktreeError, cleanup_phase_task_worktrees
REPO_PATH_PATTERN = re.compile(r"(apps/todo/[^\s'\"`:),]+|package\.json)")
POLISH_PHASE_GATE_TIMEOUT_SECONDS = 180


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
    active_bundle_ids = active_bundle_ids_for_phase(run_dir, phase)
    bundles = [read_json(path) for path in sorted((run_dir / "bundles").glob("*.json"))]
    accepted_by_objective = {}
    rejected = []
    blocked = []
    for bundle in bundles:
        if bundle["phase"] != phase:
            continue
        if str(bundle.get("bundle_id") or "").strip() not in active_bundle_ids:
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
    phase_tasks = active_phase_tasks(run_dir, phase)
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
                f"- attempt: {release_validation.get('attempt', 'none')}",
                f"- evaluation mode: {release_validation.get('evaluation_mode', 'none')}",
                f"- report: {release_validation.get('report_path', 'none')}",
                f"- summary: {release_validation.get('summary', 'none')}",
            ]
        )
        items = release_validation.get("items") or []
        lines.append(f"- checklist items: {len(items)}")
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
    report_path = phase_reports_dir / "polish-release-validation.json"
    previous = load_optional_json(report_path) or {}
    attempt = int(previous.get("attempt") or 0) + 1
    summary: dict[str, Any] = {
        "status": "running",
        "attempt": attempt,
        "evaluation_mode": "task_validation_checklist",
        "command": None,
        "working_directory": None,
        "report_path": str(report_path.relative_to(project_root)),
        "stdout_path": None,
        "stderr_path": None,
        "summary": "",
        "failure_diagnostics": [],
        "items": [],
    }
    write_json(report_path, summary)
    record_event(
        project_root,
        run_id,
        phase="polish",
        activity_id=None,
        event_type="phase.release_validation_started",
        message="Evaluating the polish validation checklist from current task state.",
        payload={"report_path": summary["report_path"], "attempt": attempt},
    )
    checklist_items = build_polish_validation_checklist_items(project_root, run_id)
    summary["items"] = [
        {
            key: item.get(key)
            for key in ("task_id", "objective_id", "capability", "validation_id", "command", "status", "summary")
        }
        for item in checklist_items
    ]
    failure_diagnostics = collect_polish_validation_diagnostics(project_root, run_id, checklist_items)
    summary["failure_diagnostics"] = failure_diagnostics
    if any(item["status"] in {"running", "pending"} for item in checklist_items):
        summary["status"] = "running"
        summary["summary"] = "Polish validation checklist is still waiting on active task work."
    elif any(item["status"] in {"failed", "blocked"} for item in checklist_items):
        summary["status"] = "failed"
        summary["summary"] = "Polish validation checklist found failing or blocked task-owned validations."
    else:
        phase_gate = run_polish_phase_release_gate(project_root, run_id, attempt=attempt)
        summary["command"] = phase_gate.get("command")
        summary["working_directory"] = phase_gate.get("working_directory")
        summary["stdout_path"] = phase_gate.get("stdout_path")
        summary["stderr_path"] = phase_gate.get("stderr_path")
        summary["items"].append(phase_gate["item"])
        summary["failure_diagnostics"].extend(phase_gate.get("failure_diagnostics", []))
        summary["status"] = str(phase_gate.get("status") or "failed")
        summary["summary"] = str(phase_gate.get("summary") or "Polish validation checklist passed.")
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


def run_polish_phase_release_gate(project_root: Path, run_id: str, *, attempt: int) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase_reports_dir = run_dir / "phase-reports"
    command = discover_polish_phase_gate_command(project_root)
    if not command:
        return {
            "status": "passed",
            "command": None,
            "working_directory": None,
            "stdout_path": None,
            "stderr_path": None,
            "summary": "Polish validation checklist passed.",
            "item": {
                "task_id": "__phase_release_gate__",
                "objective_id": None,
                "capability": None,
                "validation_id": "phase-release-gate",
                "command": None,
                "status": "passed",
                "summary": "No additional phase-level release gate was configured.",
            },
            "failure_diagnostics": [],
        }

    stdout_path = phase_reports_dir / "polish-release-validation.stdout.log"
    stderr_path = phase_reports_dir / "polish-release-validation.stderr.log"
    working_directory = str(project_root)
    record_event(
        project_root,
        run_id,
        phase="polish",
        activity_id=None,
        event_type="phase.release_gate_started",
        message="Running the phase-level polish release gate.",
        payload={"command": command, "attempt": attempt},
    )
    try:
        completed = subprocess.run(
            ["/bin/zsh", "-lc", command],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=POLISH_PHASE_GATE_TIMEOUT_SECONDS,
            check=False,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        exit_code = completed.returncode
        status = "passed" if exit_code == 0 else "failed"
        summary = (
            "Polish validation checklist passed and the phase-level release gate succeeded."
            if status == "passed"
            else f"Polish validation checklist passed, but the phase-level release gate failed with exit code {exit_code}."
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        exit_code = None
        status = "failed"
        summary = (
            "Polish validation checklist passed, but the phase-level release gate timed out "
            f"after {POLISH_PHASE_GATE_TIMEOUT_SECONDS}s."
        )
    write_text(stdout_path, stdout)
    write_text(stderr_path, stderr)
    diagnostics = []
    if status != "passed":
        diagnostics = build_phase_release_gate_diagnostics(
            project_root,
            run_id,
            command=command,
            stdout=stdout,
            stderr=stderr,
            timed_out=exit_code is None,
        )
    record_event(
        project_root,
        run_id,
        phase="polish",
        activity_id=None,
        event_type="phase.release_gate_completed",
        message=summary,
        payload={
            "command": command,
            "status": status,
            "attempt": attempt,
            "stdout_path": str(stdout_path.relative_to(project_root)),
            "stderr_path": str(stderr_path.relative_to(project_root)),
        },
    )
    return {
        "status": status,
        "command": command,
        "working_directory": working_directory,
        "stdout_path": str(stdout_path.relative_to(project_root)),
        "stderr_path": str(stderr_path.relative_to(project_root)),
        "summary": summary,
        "item": {
            "task_id": "__phase_release_gate__",
            "objective_id": None,
            "capability": None,
            "validation_id": "phase-release-gate",
            "command": command,
            "status": status,
            "summary": compact_text_block(stdout or stderr or summary, max_lines=4, max_chars=240),
        },
        "failure_diagnostics": diagnostics,
    }


def discover_polish_phase_gate_command(project_root: Path) -> str | None:
    package_path = project_root / "package.json"
    if not package_path.exists():
        return None
    try:
        package_payload = read_json(package_path)
    except Exception:
        return None
    scripts = package_payload.get("scripts")
    if not isinstance(scripts, dict):
        return None
    script_name = None
    if "validate:release-readiness" in scripts:
        script_name = "validate:release-readiness"
    elif "validate:todo-release-readiness" in scripts:
        script_name = "validate:todo-release-readiness"
    if script_name is None:
        for candidate in sorted(scripts):
            if isinstance(candidate, str) and candidate.startswith("validate:") and candidate.endswith("release-readiness"):
                script_name = candidate
                break
    return f"npm run {script_name}" if script_name else None


def build_polish_validation_checklist_items(project_root: Path, run_id: str) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    reports_dir = run_dir / "reports"
    tasks: list[dict[str, Any]] = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        payload = read_json(path)
        if payload.get("phase") == "polish":
            tasks.append(payload)
    task_by_id = {
        str(task.get("task_id") or "").strip(): task
        for task in tasks
        if str(task.get("task_id") or "").strip()
    }
    report_by_task = {path.stem: read_json(path) for path in sorted(reports_dir.glob("*.json"))}
    items: list[dict[str, Any]] = []
    for task in tasks:
        task_id = str(task.get("task_id") or "").strip()
        report = report_by_task.get(task_id)
        activity = load_optional_json(activity_path(run_dir, task_id)) or {}
        remediation_task = polish_validation_remediation_task(task, task_by_id)
        validations = list(task.get("validation") or [])
        if validations:
            results_by_id = {}
            if isinstance(report, dict):
                for item in report.get("validation_results", []):
                    if isinstance(item, dict):
                        results_by_id[str(item.get("id") or "").strip()] = item
            for validation in validations:
                if not isinstance(validation, dict):
                    continue
                validation_id = str(validation.get("id") or "").strip()
                result = results_by_id.get(validation_id)
                status = polish_checklist_item_status(report, activity, result=result)
                items.append(
                    {
                        "task_id": task_id,
                        "objective_id": task.get("objective_id"),
                        "capability": task.get("capability"),
                        "validation_id": validation_id,
                        "command": str(validation.get("command") or "").strip(),
                        "status": status,
                        "summary": polish_checklist_item_summary(report, activity, result=result),
                        "remediation_task_id": remediation_task.get("task_id"),
                    }
                )
        else:
            status = polish_checklist_item_status(report, activity, result=None)
            items.append(
                {
                    "task_id": task_id,
                    "objective_id": task.get("objective_id"),
                    "capability": task.get("capability"),
                    "validation_id": None,
                    "command": None,
                    "status": status,
                    "summary": polish_checklist_item_summary(report, activity, result=None),
                    "remediation_task_id": remediation_task.get("task_id"),
                }
            )
    return items


def polish_validation_remediation_task(
    task: dict[str, Any],
    task_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    task_id = str(task.get("task_id") or "").strip()
    if task_id.endswith("-polish-validation"):
        paired_task_id = task_id.removesuffix("-polish-validation") + "-polish-implementation"
        paired_task = task_by_id.get(paired_task_id)
        if isinstance(paired_task, dict):
            return paired_task
    return task


def polish_checklist_item_status(
    report: dict[str, Any] | None,
    activity: dict[str, Any] | None,
    *,
    result: dict[str, Any] | None,
) -> str:
    if isinstance(result, dict):
        result_status = str(result.get("status") or "").strip().lower()
        if result_status in {"passed", "failed", "blocked", "running", "pending"}:
            return result_status
    if isinstance(report, dict):
        report_status = str(report.get("status") or "").strip()
        if report_status == "ready_for_bundle_review":
            return "passed"
        if report_status in {"blocked", "failed"}:
            return report_status
    activity_status = str((activity or {}).get("status") or "").strip()
    if activity_status in {"running", "launching", "queued", "waiting_dependencies"}:
        return "running"
    if activity_status in {"blocked", "failed", "needs_revision"}:
        return "failed" if activity_status == "failed" else "blocked"
    return "pending"


def polish_checklist_item_summary(
    report: dict[str, Any] | None,
    activity: dict[str, Any] | None,
    *,
    result: dict[str, Any] | None,
) -> str:
    if isinstance(result, dict):
        for key in ("summary", "message", "detail"):
            value = str(result.get(key) or "").strip()
            if value:
                return value
    if isinstance(report, dict):
        value = str(report.get("summary") or "").strip()
        if value:
            return value
    return str((activity or {}).get("current_activity") or "").strip()


def collect_polish_validation_diagnostics(
    project_root: Path,
    run_id: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    task_by_id = {
        path.stem: read_json(path)
        for path in sorted((run_dir / "tasks").glob("*.json"))
    }
    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        status = str(item.get("status") or "").strip()
        if status not in {"failed", "blocked"}:
            continue
        task_id = str(item.get("task_id") or "").strip()
        if not task_id:
            continue
        task_path = run_dir / "tasks" / f"{task_id}.json"
        task = read_json(task_path) if task_path.exists() else {}
        report_path = run_dir / "reports" / f"{task_id}.json"
        report = read_json(report_path) if report_path.exists() else {}
        remediation_task_id = str(item.get("remediation_task_id") or "").strip()
        remediation_task = task_by_id.get(remediation_task_id) if remediation_task_id else None
        diagnostic = build_polish_validation_diagnostic(task, report, item, remediation_task=remediation_task)
        key = (
            str(diagnostic.get("task_id") or ""),
            str(diagnostic.get("category") or ""),
            str(diagnostic.get("excerpt") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(diagnostic)
    return diagnostics


def build_phase_release_gate_diagnostics(
    project_root: Path,
    run_id: str,
    *,
    command: str,
    stdout: str,
    stderr: str,
    timed_out: bool,
) -> list[dict[str, Any]]:
    block = "\n".join(part for part in (stdout, stderr) if part).strip()
    if not block:
        block = command
    objective_map = read_json(project_root / "runs" / run_id / "objective-map.json")
    owner_objectives = release_failure_owner_objectives(objective_map)
    task_lookup = polish_implementation_task_lookup(project_root, run_id)
    categories = infer_release_gate_capabilities(block)
    if not categories:
        category = "environment_blocker" if timed_out else "polish_validation_failure"
        return [
            {
                "category": category,
                "owner_capability": None,
                "owner_objective_id": None,
                "task_id": None,
                "source_test": "phase-release-gate",
                "paths": extract_release_failure_paths(block),
                "excerpt": compact_text_block(block, max_lines=6, max_chars=420),
                "repairable": False,
            }
        ]
    diagnostics: list[dict[str, Any]] = []
    for capability in sorted(categories):
        objective_id = owner_objectives.get(capability)
        task_id = task_lookup.get((objective_id or "", capability))
        diagnostics.append(
            {
                "category": "environment_blocker" if timed_out else "polish_validation_failure",
                "owner_capability": capability,
                "owner_objective_id": objective_id,
                "task_id": task_id,
                "source_test": "phase-release-gate",
                "paths": [path for path in extract_release_failure_paths(block) if capability_for_repo_path(path) == capability],
                "excerpt": compact_text_block(block, max_lines=6, max_chars=420),
                "repairable": (not timed_out) and bool(objective_id) and bool(task_id),
            }
        )
    return diagnostics


def infer_release_gate_capabilities(block: str) -> set[str]:
    capabilities = {
        capability
        for capability in (
            capability_for_repo_path(path)
            for path in extract_release_failure_paths(block)
        )
        if capability
    }
    if capabilities:
        return capabilities
    lowered = block.lower()
    inferred: set[str] = set()
    if "frontend" in lowered:
        inferred.add("frontend")
    if "backend" in lowered:
        inferred.add("backend")
    if "runtime" in lowered or "middleware" in lowered:
        inferred.add("middleware")
    return inferred


def capability_for_repo_path(path: str) -> str | None:
    normalized = str(path).strip()
    if normalized.startswith("apps/todo/frontend/"):
        return "frontend"
    if normalized.startswith("apps/todo/backend/"):
        return "backend"
    if normalized.startswith("apps/todo/runtime/"):
        return "middleware"
    return None


def polish_implementation_task_lookup(project_root: Path, run_id: str) -> dict[tuple[str, str], str]:
    lookup: dict[tuple[str, str], str] = {}
    for path in sorted((project_root / "runs" / run_id / "tasks").glob("*.json")):
        task = read_json(path)
        if task.get("phase") != "polish":
            continue
        task_id = str(task.get("task_id") or "").strip()
        objective_id = str(task.get("objective_id") or "").strip()
        capability = str(task.get("capability") or "").strip()
        if not task_id or not objective_id or not capability:
            continue
        if task_id.endswith("-polish-implementation"):
            lookup[(objective_id, capability)] = task_id
    return lookup


def build_polish_validation_diagnostic(
    task: dict[str, Any],
    report: dict[str, Any],
    item: dict[str, Any],
    *,
    remediation_task: dict[str, Any] | None = None,
) -> dict[str, Any]:
    excerpt = str(item.get("summary") or report.get("summary") or "").strip()
    lowered = excerpt.lower()
    target_task = remediation_task if isinstance(remediation_task, dict) and remediation_task else task
    paths = [
        str(value).strip()
        for value in target_task.get("owned_paths", [])
        if isinstance(value, str) and str(value).strip()
    ]
    if not paths:
        paths = extract_release_failure_paths(excerpt)
    environment_markers = (
        "operation not permitted",
        "permission denied",
        "read-only file system",
        "sandbox",
        "mktemp",
        "tmpdir",
        "eacces",
    )
    category = "environment_blocker" if any(marker in lowered for marker in environment_markers) else "polish_validation_failure"
    return {
        "category": category,
        "owner_capability": target_task.get("capability"),
        "owner_objective_id": target_task.get("objective_id"),
        "task_id": target_task.get("task_id"),
        "source_test": item.get("validation_id") or task.get("task_id"),
        "paths": paths,
        "excerpt": compact_text_block(excerpt or f"{task.get('task_id')} {item.get('status')}", max_lines=4, max_chars=420),
        "repairable": category != "environment_blocker" and bool(target_task.get("objective_id")) and bool(target_task.get("task_id")),
    }


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
    if any(path.startswith("apps/todo/frontend/") for path in paths):
        return "frontend"
    if any(path.startswith("apps/todo/backend/") for path in paths):
        return "backend"
    if any(path.startswith("apps/todo/runtime/") for path in paths):
        return "middleware"
    return None


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


def advance_phase(project_root: Path, run_id: str, *, bypass_human_approval: bool = False) -> dict[str, Any]:
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
    if not bypass_human_approval and not phase_state.get("human_approved"):
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
