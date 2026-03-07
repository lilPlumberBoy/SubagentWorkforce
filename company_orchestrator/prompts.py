from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .filesystem import read_json, read_text, write_json, write_text
from .planner import assert_active_phase


def render_prompt(project_root: Path, run_id: str, task_path: Path) -> dict[str, Any]:
    task = read_json(task_path)
    run_dir = project_root / "runs" / run_id
    assert_active_phase(project_root, run_id, task["phase"])
    assigned_role = task["assigned_role"]
    role_name = assigned_role.split(".")[-1]
    role_kind = _infer_role_kind(role_name)
    files_loaded: list[str] = []
    parts: list[str] = []

    def add(path: Path) -> None:
        parts.append(read_text(path))
        files_loaded.append(str(path.relative_to(project_root)))

    add(project_root / "orchestrator" / "roles" / "base" / "company.md")
    add(project_root / "orchestrator" / "roles" / "base" / f"{role_kind}.md")

    capability = task.get("capability")
    capability_path = project_root / "orchestrator" / "roles" / "capabilities" / f"{capability}.md"
    if capability and capability_path.exists():
        add(capability_path)

    objective_id = task["objective_id"]
    objective_root = project_root / "orchestrator" / "roles" / "objectives" / objective_id
    role_path = objective_root / "approved" / f"{role_name}.md"
    if role_path.exists():
        add(role_path)
    else:
        add(objective_root / "charter.md")

    phase_path = project_root / "orchestrator" / "phase-overlays" / f"{task['phase']}.md"
    add(phase_path)

    rendered_task = json.dumps(task, indent=2, sort_keys=True)
    prompt_path = run_dir / "prompt-logs" / f"{task['task_id']}.prompt.md"
    log_path = run_dir / "prompt-logs" / f"{task['task_id']}.json"
    metadata = {
        "task_id": task["task_id"],
        "assigned_role": assigned_role,
        "role_kind": role_kind,
        "objective_id": objective_id,
        "phase": task["phase"],
        "schema": task["schema"],
        "files_loaded": files_loaded,
        "prompt_path": str(prompt_path.relative_to(project_root)),
    }
    runtime_context = {
        "prompt_layers_loaded": files_loaded,
        "prompt_log_path": metadata["prompt_path"],
        "role_kind": role_kind,
    }
    prompt_text = "\n\n".join(
        parts
        + [
            "# Runtime Context\n\n```json\n" + json.dumps(runtime_context, indent=2, sort_keys=True) + "\n```",
            f"# Task Assignment\n\n```json\n{rendered_task}\n```",
        ]
    )
    write_text(prompt_path, prompt_text)
    write_json(log_path, metadata)
    return metadata


def render_objective_planning_prompt(project_root: Path, run_id: str, objective_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase_plan = read_json(run_dir / "phase-plan.json")
    phase = phase_plan["current_phase"]
    objective_map = read_json(run_dir / "objective-map.json")
    team_registry = read_json(run_dir / "team-registry.json")
    objective = next(item for item in objective_map["objectives"] if item["objective_id"] == objective_id)
    team = next(item for item in team_registry["teams"] if item["objective_id"] == objective_id)
    files_loaded: list[str] = []
    parts: list[str] = []

    def add(path: Path) -> None:
        parts.append(read_text(path))
        files_loaded.append(str(path.relative_to(project_root)))

    add(project_root / "orchestrator" / "roles" / "base" / "company.md")
    add(project_root / "orchestrator" / "roles" / "base" / "manager.md")
    add(project_root / "orchestrator" / "roles" / "base" / "objective-manager.md")

    objective_root = project_root / "orchestrator" / "roles" / "objectives" / objective_id
    add(objective_root / "charter.md")
    objective_manager_path = objective_root / "approved" / "objective-manager.md"
    if objective_manager_path.exists():
        add(objective_manager_path)

    add(project_root / "orchestrator" / "phase-overlays" / f"{phase}.md")

    runtime_context = {
        "prompt_layers_loaded": files_loaded,
        "planning_schema": "objective-plan.v1",
        "objective_id": objective_id,
        "phase": phase,
        "available_roles": [f"objectives.{objective_id}.{role['role_id']}" for role in team["roles"]],
        "worker_roles": [
            f"objectives.{objective_id}.{role['role_id']}" for role in team["roles"] if role["role_kind"] == "worker"
        ],
    }
    payload = {
        "goal_markdown": read_text(run_dir / "goal.md"),
        "objective": objective,
        "team": team,
        "existing_phase_tasks": [
            read_json(path)
            for path in sorted((run_dir / "tasks").glob("*.json"))
            if read_json(path)["phase"] == phase and read_json(path)["objective_id"] == objective_id
        ],
    }
    prompt_text = "\n\n".join(
        parts
        + [
            "# Runtime Context\n\n```json\n" + json.dumps(runtime_context, indent=2, sort_keys=True) + "\n```",
            "# Planning Inputs\n\n```json\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n```",
        ]
    )
    prompt_path = run_dir / "manager-plans" / f"{phase}-{objective_id}.prompt.md"
    log_path = run_dir / "manager-plans" / f"{phase}-{objective_id}.prompt.json"
    write_text(prompt_path, prompt_text)
    metadata = {
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "files_loaded": files_loaded,
        "prompt_path": str(prompt_path.relative_to(project_root)),
    }
    write_json(log_path, metadata)
    return metadata


def _infer_role_kind(role_name: str) -> str:
    if role_name == "acceptance-manager":
        return "acceptance-manager"
    if role_name.endswith("manager"):
        return "manager"
    return "worker"
