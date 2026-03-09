from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .executor import (
    ExecutorError,
    build_codex_command,
    build_exec_environment,
    coerce_process_text,
    extract_final_response,
    extract_thread_id,
    extract_turn_failure,
    extract_usage,
    parse_jsonl_events,
)
from .filesystem import ensure_dir, read_json, write_json, write_text
from .objective_roots import find_objective_root
from .prompts import preview_resolved_inputs, render_objective_planning_prompt
from .schemas import SchemaValidationError, validate_document


def plan_objective(
    project_root: Path,
    run_id: str,
    objective_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    replace: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase = read_json(run_dir / "phase-plan.json")["current_phase"]
    objective = find_objective(run_dir, objective_id)
    prompt_metadata = render_objective_planning_prompt(project_root, run_id, objective_id)
    prompt_text = (project_root / prompt_metadata["prompt_path"]).read_text(encoding="utf-8")
    execution_prompt = build_planning_prompt(prompt_text)
    plans_dir = ensure_dir(run_dir / "manager-plans")
    output_schema_path = project_root / "orchestrator" / "schemas" / "objective-plan.v1.json"
    last_message_path = plans_dir / f"{phase}-{objective_id}.last-message.json"
    stdout_path = plans_dir / f"{phase}-{objective_id}.stdout.jsonl"
    stderr_path = plans_dir / f"{phase}-{objective_id}.stderr.log"

    command = build_codex_command(
        codex_path=codex_path,
        working_directory=project_root,
        output_schema_path=output_schema_path,
        last_message_path=last_message_path,
        sandbox_mode=sandbox_mode,
        additional_directories=[],
    )
    try:
        completed = subprocess.run(
            command,
            input=execution_prompt,
            text=True,
            capture_output=True,
            cwd=project_root,
            env=build_exec_environment(),
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = coerce_process_text(exc.stdout)
        stderr = coerce_process_text(exc.stderr)
        write_text(stdout_path, stdout)
        write_text(stderr_path, stderr)
        raise ExecutorError(
            f"codex exec timed out after {timeout_seconds} seconds while planning objective {objective_id}"
        ) from exc
    write_text(stdout_path, completed.stdout)
    write_text(stderr_path, completed.stderr)
    events = parse_jsonl_events(completed.stdout)
    failure = extract_turn_failure(events)
    if completed.returncode != 0 or failure is not None:
        message = failure or completed.stderr.strip() or f"codex exec exited with code {completed.returncode}"
        raise ExecutorError(message)

    final_response = extract_final_response(events)
    try:
        plan = json.loads(final_response)
    except json.JSONDecodeError as exc:
        raise ExecutorError(f"Objective plan was not valid JSON: {final_response}") from exc

    try:
        validate_document(plan, "objective-plan.v1", project_root)
    except SchemaValidationError as exc:
        raise ExecutorError(f"Objective plan failed schema validation: {exc}") from exc

    identity_adjustments = normalize_plan_identity(plan, run_id=run_id, phase=phase, objective_id=objective_id)
    normalize_bundle_ids(plan)

    validate_objective_plan_contents(plan, objective)
    validate_objective_plan_inputs(project_root, run_id, plan)
    materialize_objective_plan(project_root, run_id, plan, replace=replace)

    summary = {
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "thread_id": extract_thread_id(events),
        "usage": extract_usage(events),
        "plan_path": f"runs/{run_id}/manager-plans/{phase}-{objective_id}.json",
        "task_ids": [task["task_id"] for task in plan["tasks"]],
        "bundle_ids": [bundle["bundle_id"] for bundle in plan["bundle_plan"]],
        "stdout_path": str(stdout_path.relative_to(project_root)),
        "stderr_path": str(stderr_path.relative_to(project_root)),
        "last_message_path": str(last_message_path.relative_to(project_root)),
        "identity_adjustments": identity_adjustments,
    }
    write_json(plans_dir / f"{phase}-{objective_id}.summary.json", summary)
    return summary


def plan_phase(
    project_root: Path,
    run_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    replace: bool = False,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase = read_json(run_dir / "phase-plan.json")["current_phase"]
    objective_map = read_json(run_dir / "objective-map.json")
    summaries = []
    for objective in objective_map["objectives"]:
        summaries.append(
            plan_objective(
                project_root,
                run_id,
                objective["objective_id"],
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                replace=replace,
                timeout_seconds=timeout_seconds,
            )
        )
    payload = {"run_id": run_id, "phase": phase, "planned_objectives": summaries}
    write_json(run_dir / "manager-plans" / f"{phase}-phase-plan-summary.json", payload)
    return payload


def find_objective(run_dir: Path, objective_id: str) -> dict[str, Any]:
    objective_map = read_json(run_dir / "objective-map.json")
    for objective in objective_map["objectives"]:
        if objective["objective_id"] == objective_id:
            return objective
    raise ValueError(f"Objective {objective_id} was not found")


def build_planning_prompt(prompt_text: str) -> str:
    return (
        prompt_text
        + "\n\n# Objective Planning Output Requirements\n\n"
        + "Return only one JSON object matching the objective-plan schema.\n"
        + "Copy run_id, phase, and objective_id exactly from the injected Runtime Context and Planning Inputs.\n"
        + "Use only the Runtime Context and Planning Inputs already provided in this prompt.\n"
        + "Do not inspect the repository, run shell commands, or read additional files.\n"
        + "Do not perform exploratory analysis outside the injected planning inputs.\n"
        + "Return the JSON plan as your first and only response.\n"
        + "Do not execute implementation work.\n"
        + "Produce small isolated tasks for the active phase only.\n"
        + "Use only worker roles from the listed objective team when assigning tasks.\n"
        + "Every bundle in bundle_plan must reference only generated task ids.\n"
        + "For phases after discovery, each task input must be either a concrete repo-relative file path, "
        + "an explicit `Output of <task-id>` reference, or a dotted `Planning Inputs.`/`Runtime Context.` reference.\n"
        + "When prior-phase reports or artifacts are available in Planning Inputs, prefer referencing those exact paths "
        + "instead of vague English placeholders such as 'approved design package'.\n"
    )


def normalize_plan_identity(
    plan: dict[str, Any], *, run_id: str, phase: str, objective_id: str
) -> dict[str, dict[str, str]]:
    if plan["phase"] != phase or plan["objective_id"] != objective_id:
        raise ExecutorError("Objective plan identity does not match the requested objective/phase")
    adjustments: dict[str, dict[str, str]] = {}
    if plan["run_id"] != run_id:
        adjustments["run_id"] = {"from": str(plan["run_id"]), "to": run_id}
        plan["run_id"] = run_id
    return adjustments


def normalize_bundle_ids(plan: dict[str, Any]) -> None:
    objective_id = plan["objective_id"]
    seen: set[str] = set()
    for bundle in plan["bundle_plan"]:
        bundle_id = bundle["bundle_id"]
        if not bundle_id.startswith(f"{objective_id}-"):
            bundle_id = f"{objective_id}-{bundle_id}"
            bundle["bundle_id"] = bundle_id
        if bundle_id in seen:
            raise ExecutorError(f"Objective plan duplicated bundle id {bundle_id}")
        seen.add(bundle_id)


def validate_objective_plan_contents(plan: dict[str, Any], objective: dict[str, Any]) -> None:
    valid_roles = set()
    for capability in objective["capabilities"]:
        if capability == "general":
            valid_roles.add(f"objectives.{objective['objective_id']}.general-worker")
        else:
            valid_roles.add(f"objectives.{objective['objective_id']}.{capability}-worker")
    valid_roles.add(f"objectives.{objective['objective_id']}.general-worker")
    task_ids = set()
    for task in plan["tasks"]:
        if task["task_id"] in task_ids:
            raise ExecutorError(f"Objective plan duplicated task id {task['task_id']}")
        task_ids.add(task["task_id"])
        if task["assigned_role"] not in valid_roles:
            raise ExecutorError(f"Objective plan assigned unknown worker role {task['assigned_role']}")
    for bundle in plan["bundle_plan"]:
        for task_id in bundle["task_ids"]:
            if task_id not in task_ids:
                raise ExecutorError(f"Bundle {bundle['bundle_id']} referenced unknown task {task_id}")


def validate_objective_plan_inputs(project_root: Path, run_id: str, plan: dict[str, Any]) -> None:
    for planned_task in plan["tasks"]:
        preview_task = {
            "schema": "task-assignment.v1",
            "run_id": run_id,
            "phase": plan["phase"],
            "objective_id": plan["objective_id"],
            "capability": planned_task["capability"],
            "working_directory": planned_task["working_directory"],
            "sandbox_mode": planned_task["sandbox_mode"],
            "additional_directories": planned_task["additional_directories"],
            "task_id": planned_task["task_id"],
            "assigned_role": planned_task["assigned_role"],
            "manager_role": derive_manager_role(project_root, plan["objective_id"], planned_task["assigned_role"]),
            "acceptance_role": f"objectives.{plan['objective_id']}.acceptance-manager",
            "objective": planned_task["objective"],
            "inputs": planned_task["inputs"],
            "expected_outputs": planned_task["expected_outputs"],
            "done_when": planned_task["done_when"],
            "depends_on": planned_task["depends_on"],
            "validation": planned_task["validation"],
            "collaboration_rules": planned_task["collaboration_rules"],
        }
        unresolved = sorted(collect_unresolved_input_refs(preview_resolved_inputs(project_root, run_id, preview_task)))
        if unresolved:
            raise ExecutorError(
                f"Objective plan produced unresolved input refs for task {planned_task['task_id']}: "
                + ", ".join(unresolved)
            )


def collect_unresolved_input_refs(value: Any) -> set[str]:
    unresolved: set[str] = set()
    if isinstance(value, dict):
        unresolved_ref = value.get("unresolved_input_ref")
        if isinstance(unresolved_ref, str):
            unresolved.add(unresolved_ref)
        for nested in value.values():
            unresolved.update(collect_unresolved_input_refs(nested))
    elif isinstance(value, list):
        for nested in value:
            unresolved.update(collect_unresolved_input_refs(nested))
    return unresolved


def materialize_objective_plan(project_root: Path, run_id: str, plan: dict[str, Any], *, replace: bool) -> None:
    run_dir = project_root / "runs" / run_id
    phase = plan["phase"]
    objective_id = plan["objective_id"]
    existing_paths = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        payload = read_json(path)
        if payload["phase"] == phase and payload["objective_id"] == objective_id:
            existing_paths.append(path)
    if existing_paths and not replace:
        raise ExecutorError(f"Tasks already exist for objective {objective_id} in phase {phase}; rerun with replace")
    for path in existing_paths:
        path.unlink()

    manager_plan_path = run_dir / "manager-plans" / f"{phase}-{objective_id}.json"
    write_json(manager_plan_path, plan)

    for planned_task in plan["tasks"]:
        payload = {
            "schema": "task-assignment.v1",
            "run_id": run_id,
            "phase": phase,
            "objective_id": objective_id,
            "capability": planned_task["capability"],
            "working_directory": planned_task["working_directory"],
            "sandbox_mode": planned_task["sandbox_mode"],
            "additional_directories": planned_task["additional_directories"],
            "task_id": planned_task["task_id"],
            "assigned_role": planned_task["assigned_role"],
            "manager_role": derive_manager_role(project_root, objective_id, planned_task["assigned_role"]),
            "acceptance_role": f"objectives.{objective_id}.acceptance-manager",
            "objective": planned_task["objective"],
            "inputs": planned_task["inputs"],
            "expected_outputs": planned_task["expected_outputs"],
            "done_when": planned_task["done_when"],
            "depends_on": planned_task["depends_on"],
            "validation": planned_task["validation"],
            "collaboration_rules": planned_task["collaboration_rules"],
        }
        validate_document(payload, "task-assignment.v1", project_root)
        write_json(run_dir / "tasks" / f"{planned_task['task_id']}.json", payload)


def derive_manager_role(project_root: Path, objective_id: str, assigned_role: str) -> str:
    role_name = assigned_role.split(".")[-1]
    if role_name == "general-worker":
        return f"objectives.{objective_id}.objective-manager"
    candidate_role_name = role_name.replace("-worker", "-manager")
    candidate_path = find_objective_root(project_root, objective_id) / "approved" / f"{candidate_role_name}.md"
    if candidate_path.exists():
        return f"objectives.{objective_id}.{candidate_role_name}"
    return f"objectives.{objective_id}.objective-manager"
