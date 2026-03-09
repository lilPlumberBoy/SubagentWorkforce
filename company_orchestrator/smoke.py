from __future__ import annotations

from pathlib import Path
from typing import Any

from .bundles import assemble_review_bundle, review_bundle
from .filesystem import read_json, write_json
from .planner import generate_role_files, initialize_run, suggest_team_proposals
from .prompts import render_prompt
from .schemas import validate_document


SMOKE_GOAL = """# Smoke Test Goal

## Objectives
- App A context verification
- App B context verification
"""


def scaffold_smoke_test(project_root: Path, run_id: str = "smoke-demo") -> Path:
    run_dir = initialize_run(project_root, run_id, SMOKE_GOAL)
    objective_map = {
        "schema": "objective-map.v1",
        "run_id": run_id,
        "objectives": [
            {"objective_id": "app-a", "title": "App A context verification", "summary": "App A context verification", "status": "approved", "capabilities": ["frontend"]},
            {"objective_id": "app-b", "title": "App B context verification", "summary": "App B context verification", "status": "approved", "capabilities": ["backend"]},
        ],
        "dependencies": [],
    }
    write_json(run_dir / "objective-map.json", objective_map)
    suggest_team_proposals(project_root, run_id)
    generate_role_files(project_root, run_id, approve=True)

    tasks = [
        smoke_task(run_id, "app-a", "frontend", "APP-A-SMOKE-001"),
        smoke_task(run_id, "app-b", "backend", "APP-B-SMOKE-001"),
    ]
    for task in tasks:
        validate_document(task, "task-assignment.v1", project_root)
        write_json(run_dir / "tasks" / f"{task['task_id']}.json", task)
        render_prompt(project_root, run_id, run_dir / "tasks" / f"{task['task_id']}.json")
    return run_dir


def smoke_task(run_id: str, objective_id: str, capability: str, task_id: str) -> dict[str, Any]:
    return {
        "schema": "task-assignment.v1",
        "run_id": run_id,
        "phase": "discovery",
        "objective_id": objective_id,
        "capability": capability,
        "task_id": task_id,
        "assigned_role": f"objectives.{objective_id}.{capability}-worker",
        "manager_role": f"objectives.{objective_id}.objective-manager",
        "acceptance_role": f"objectives.{objective_id}.acceptance-manager",
        "objective": "Echo prompt context for smoke verification.",
        "inputs": [],
        "expected_outputs": ["completion-report.v1"],
        "done_when": [
            "role id is echoed",
            "objective id is echoed",
            "phase is echoed",
            "loaded prompt layers are echoed",
        ],
        "execution_mode": "read_only",
        "parallel_policy": "allow",
        "owned_paths": [],
        "shared_asset_ids": [],
        "depends_on": [],
        "validation": [{"id": "context-echo", "command": "mock-runtime"}],
        "collaboration_rules": [],
    }


def simulate_context_echo_completion(project_root: Path, run_id: str, task_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    task = read_json(run_dir / "tasks" / f"{task_id}.json")
    prompt_log = read_json(run_dir / "prompt-logs" / f"{task_id}.json")
    report = {
        "schema": "completion-report.v1",
        "run_id": run_id,
        "phase": task["phase"],
        "objective_id": task["objective_id"],
        "task_id": task_id,
        "agent_role": task["assigned_role"],
        "status": "ready_for_bundle_review",
        "summary": "Context echo simulation complete.",
        "artifacts": [{"path": prompt_log["prompt_path"], "status": "referenced"}],
        "validation_results": [{"id": "context-echo", "status": "passed", "evidence": "mock-runtime returned expected fields"}],
        "dependency_impact": [],
        "open_issues": [],
        "follow_up_requests": [],
        "context_echo": {
            "role_id": task["assigned_role"],
            "objective_id": task["objective_id"],
            "phase": task["phase"],
            "prompt_layers": prompt_log["files_loaded"],
            "schema": task["schema"],
        },
    }
    validate_document(report, "completion-report.v1", project_root)
    write_json(run_dir / "reports" / f"{task_id}.json", report)
    return report


def verify_smoke_reports(project_root: Path, run_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    for task_path in sorted((run_dir / "tasks").glob("*.json")):
        task_id = task_path.stem
        if not (run_dir / "reports" / f"{task_id}.json").exists():
            simulate_context_echo_completion(project_root, run_id, task_id)
    accepted = {}
    for task_path in sorted((run_dir / "tasks").glob("*.json")):
        task = read_json(task_path)
        task_id = task["task_id"]
        bundle_id = f"{task['objective_id']}-bundle"
        assemble_review_bundle(
            project_root,
            run_id,
            bundle_id,
            [run_dir / "reports" / f"{task_id}.json"],
            f"objectives.{task['objective_id']}.objective-manager",
            f"objectives.{task['objective_id']}.acceptance-manager",
        )
        bundle = review_bundle(project_root, run_id, bundle_id)
        if bundle["status"] != "accepted":
            raise ValueError(f"Smoke bundle failed: {bundle['rejection_reasons']}")
        accepted[bundle_id] = bundle["status"]
    return {"status": "accepted", "bundles": accepted}
