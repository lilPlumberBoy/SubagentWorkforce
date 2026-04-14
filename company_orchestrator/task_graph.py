from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .filesystem import load_optional_json, read_json, write_json
from .live import now_timestamp
from .objective_roots import capability_workspace_root, find_objective_app_root
from .output_descriptors import descriptor_path, normalize_output_descriptors
from .worktree_manager import integration_workspace_path


def objective_task_graph_manifest_path(run_dir: Path, phase: str, objective_id: str) -> Path:
    return run_dir / "manager-plans" / f"{phase}-{objective_id}.active.json"


def run_file_graph_path(run_dir: Path) -> Path:
    return run_dir / "live" / "run-file-graph.json"


def _empty_run_file_graph(run_id: str) -> dict[str, Any]:
    return {
        "schema": "run-file-graph.v1",
        "run_id": run_id,
        "updated_at": now_timestamp(),
        "phases": {},
    }


def load_run_file_graph(run_dir: Path) -> dict[str, Any] | None:
    return load_optional_json(run_file_graph_path(run_dir))


def _suffix_mapping_for_language(language: str | None) -> dict[str, str]:
    if language == "javascript":
        return {
            ".ts": ".js",
            ".tsx": ".jsx",
            ".cts": ".cjs",
            ".mts": ".mjs",
        }
    if language == "typescript":
        return {
            ".js": ".ts",
            ".jsx": ".tsx",
            ".cjs": ".cts",
            ".mjs": ".mts",
        }
    return {}


def _normalize_repo_relative_string(value: str) -> str:
    return str(value).strip().replace("\\", "/")


def infer_validation_runtime_requirements(command: str) -> dict[str, bool]:
    normalized = " ".join(str(command).strip().split())
    lower = normalized.lower()
    requires_writable_temp = False
    requires_writable_workspace = False
    if "--test" in lower:
        requires_writable_temp = True
    if lower.startswith("npm test") or " npm test" in lower:
        requires_writable_temp = True
    if lower.startswith("npm run validate:") or " npm run validate:" in lower:
        requires_writable_temp = True
    return {
        "requires_writable_temp": requires_writable_temp,
        "requires_writable_workspace": requires_writable_workspace,
    }


def infer_task_runtime_requirements(task: dict[str, Any]) -> dict[str, Any]:
    requirements = {
        "declares_concrete_file_outputs": False,
        "requires_writable_temp": False,
        "requires_writable_workspace": False,
    }
    for descriptor in normalize_output_descriptors(list(task.get("expected_outputs", []))):
        path_value = descriptor_path(descriptor)
        if path_value:
            requirements["declares_concrete_file_outputs"] = True
            break
    write_paths = [
        _normalize_repo_relative_string(value)
        for value in task.get("writes_existing_paths", [])
        if _normalize_repo_relative_string(value)
    ]
    if write_paths:
        requirements["requires_writable_workspace"] = True
    for validation in task.get("validation", []):
        command = validation.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        validation_requirements = infer_validation_runtime_requirements(command)
        requirements["requires_writable_temp"] = (
            requirements["requires_writable_temp"] or validation_requirements["requires_writable_temp"]
        )
        requirements["requires_writable_workspace"] = (
            requirements["requires_writable_workspace"] or validation_requirements["requires_writable_workspace"]
        )
    return requirements


def load_task_runtime_contract(
    run_dir: Path,
    *,
    phase: str,
    objective_id: str,
    capability: str,
    task_id: str,
) -> dict[str, Any] | None:
    graph = load_run_file_graph(run_dir)
    if not isinstance(graph, dict):
        return None
    return (
        graph.get("phases", {})
        .get(phase, {})
        .get("objectives", {})
        .get(objective_id, {})
        .get("capabilities", {})
        .get(capability, {})
        .get("accepted_plan_task_files", {})
        .get(task_id)
    )


def detect_capability_workspace_language(
    project_root: Path,
    *,
    run_id: str,
    objective_id: str,
    capability: str,
    phase: str,
) -> str | None:
    app_root = find_objective_app_root(project_root, objective_id)
    if app_root is None:
        return None
    workspace_root = capability_workspace_root(app_root, capability, phase=phase)
    if workspace_root is None:
        return None
    try:
        relative_workspace_root = workspace_root.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    roots = [project_root]
    integration_root = integration_workspace_path(project_root, run_id)
    if integration_root.exists():
        roots.append(integration_root)
    js_files = 0
    ts_files = 0
    seen: set[str] = set()
    for root in roots:
        candidate_root = root / relative_workspace_root
        if not candidate_root.exists():
            continue
        for candidate in candidate_root.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                relative_path = str(candidate.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if relative_path in seen:
                continue
            seen.add(relative_path)
            if candidate.suffix in {".js", ".jsx", ".cjs", ".mjs"}:
                js_files += 1
            elif candidate.suffix in {".ts", ".tsx", ".cts", ".mts"}:
                ts_files += 1
    if js_files and not ts_files:
        return "javascript"
    if ts_files and not js_files:
        return "typescript"
    return None


def _workspace_prefix_for_capability(
    project_root: Path,
    *,
    objective_id: str,
    capability: str,
    phase: str,
) -> str | None:
    app_root = find_objective_app_root(project_root, objective_id)
    if app_root is None:
        return None
    workspace_root = capability_workspace_root(app_root, capability, phase=phase)
    if workspace_root is None:
        return None
    try:
        return str(workspace_root.resolve().relative_to(project_root.resolve())).replace("\\", "/")
    except ValueError:
        return None


def _build_output_path_mapping(
    descriptors: list[dict[str, Any]],
    *,
    workspace_prefix: str | None,
    language: str | None,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not workspace_prefix:
        return mapping
    prefix = workspace_prefix.rstrip("/") + "/"
    suffix_mapping = _suffix_mapping_for_language(language)
    for descriptor in normalize_output_descriptors(list(descriptors)):
        path_value = descriptor_path(descriptor)
        if not path_value:
            continue
        normalized = _normalize_repo_relative_string(path_value)
        if not normalized.startswith(prefix):
            continue
        target_suffix = suffix_mapping.get(Path(normalized).suffix)
        if not target_suffix:
            continue
        replacement = str(Path(normalized).with_suffix(target_suffix)).replace("\\", "/")
        if replacement != normalized:
            mapping[normalized] = replacement
    return mapping


def _apply_path_mapping(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _apply_path_mapping(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_apply_path_mapping(item, mapping) for item in value]
    if isinstance(value, str):
        updated = value
        for source, target in mapping.items():
            updated = updated.replace(source, target)
        return updated
    return copy.deepcopy(value)


def normalize_capability_contract_for_run(
    project_root: Path,
    run_id: str,
    *,
    objective_id: str,
    capability: str,
    phase: str,
    capability_lane: dict[str, Any],
    objective_outline: dict[str, Any],
    required_outbound_handoffs: list[dict[str, Any]],
) -> dict[str, Any]:
    workspace_language = detect_capability_workspace_language(
        project_root,
        run_id=run_id,
        objective_id=objective_id,
        capability=capability,
        phase=phase,
    )
    workspace_prefix = _workspace_prefix_for_capability(
        project_root,
        objective_id=objective_id,
        capability=capability,
        phase=phase,
    )
    path_mapping = _build_output_path_mapping(
        list(capability_lane.get("expected_outputs", [])),
        workspace_prefix=workspace_prefix,
        language=workspace_language,
    )
    normalized_lane = _apply_path_mapping(capability_lane, path_mapping)
    normalized_outline = _apply_path_mapping(objective_outline, path_mapping)
    normalized_handoffs = _apply_path_mapping(required_outbound_handoffs, path_mapping)
    return {
        "workspace_language": workspace_language,
        "workspace_prefix": workspace_prefix,
        "path_mapping": path_mapping,
        "capability_lane": normalized_lane,
        "objective_outline": normalized_outline,
        "required_outbound_handoffs": normalized_handoffs,
        "required_final_outputs": normalize_output_descriptors(list(normalized_lane.get("expected_outputs", []))),
    }


def update_run_file_graph_contract(
    run_dir: Path,
    *,
    phase: str,
    objective_id: str,
    capability: str,
    workspace_language: str | None,
    workspace_prefix: str | None,
    path_mapping: dict[str, str],
    required_final_outputs: list[dict[str, Any]],
    required_outbound_handoffs: list[dict[str, Any]],
) -> dict[str, Any]:
    graph = load_run_file_graph(run_dir) or _empty_run_file_graph(run_dir.name)
    phases = graph.setdefault("phases", {})
    phase_state = phases.setdefault(phase, {"objectives": {}})
    objectives = phase_state.setdefault("objectives", {})
    objective_state = objectives.setdefault(objective_id, {"capabilities": {}})
    capabilities = objective_state.setdefault("capabilities", {})
    capability_state = capabilities.setdefault(capability, {})
    capability_state.update(
        {
            "workspace_language": workspace_language,
            "workspace_prefix": workspace_prefix,
            "path_mapping": dict(path_mapping),
            "required_final_outputs": normalize_output_descriptors(list(required_final_outputs)),
            "required_outbound_handoffs": copy.deepcopy(required_outbound_handoffs),
            "updated_at": now_timestamp(),
        }
    )
    graph["updated_at"] = now_timestamp()
    write_json(run_file_graph_path(run_dir), graph)
    return capability_state


def update_run_file_graph_capability_plan(
    run_dir: Path,
    *,
    phase: str,
    objective_id: str,
    capability: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    graph = load_run_file_graph(run_dir) or _empty_run_file_graph(run_dir.name)
    phases = graph.setdefault("phases", {})
    phase_state = phases.setdefault(phase, {"objectives": {}})
    objectives = phase_state.setdefault("objectives", {})
    objective_state = objectives.setdefault(objective_id, {"capabilities": {}})
    capabilities = objective_state.setdefault("capabilities", {})
    capability_state = capabilities.setdefault(capability, {})
    task_files: dict[str, dict[str, Any]] = {}
    for task in plan.get("tasks", []):
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            continue
        runtime_requirements = infer_task_runtime_requirements(task)
        task_files[task_id] = {
            "execution_mode": str(task.get("execution_mode") or "").strip() or None,
            "expected_outputs": normalize_output_descriptors(list(task.get("expected_outputs", []))),
            "writes_existing_paths": [
                _normalize_repo_relative_string(value)
                for value in task.get("writes_existing_paths", [])
                if _normalize_repo_relative_string(value)
            ],
            "owned_paths": [
                _normalize_repo_relative_string(value)
                for value in task.get("owned_paths", [])
                if _normalize_repo_relative_string(value)
            ],
            "runtime_requirements": runtime_requirements,
        }
    capability_state["accepted_plan_task_files"] = task_files
    capability_state["accepted_plan_path"] = (
        f"runs/{run_dir.name}/manager-plans/{phase}-{objective_id}-{capability}.json"
    )
    capability_state["updated_at"] = now_timestamp()
    graph["updated_at"] = now_timestamp()
    write_json(run_file_graph_path(run_dir), graph)
    return capability_state


def write_objective_task_graph_manifest(
    run_dir: Path,
    *,
    phase: str,
    objective_id: str,
    task_ids: list[str],
    bundle_ids: list[str],
    handoff_ids: list[str],
) -> dict[str, Any]:
    payload = {
        "run_id": run_dir.name,
        "phase": phase,
        "objective_id": objective_id,
        "task_ids": list(task_ids),
        "bundle_ids": list(bundle_ids),
        "handoff_ids": list(handoff_ids),
        "updated_at": now_timestamp(),
    }
    write_json(objective_task_graph_manifest_path(run_dir, phase, objective_id), payload)
    return payload


def load_objective_task_graph_manifest(run_dir: Path, phase: str, objective_id: str) -> dict[str, Any] | None:
    return load_optional_json(objective_task_graph_manifest_path(run_dir, phase, objective_id))


def active_task_ids_by_objective_for_phase(run_dir: Path, phase: str) -> dict[str, set[str]]:
    manager_dir = run_dir / "manager-plans"
    if not manager_dir.exists():
        return {}
    task_ids_by_objective: dict[str, set[str]] = {}
    for path in sorted(manager_dir.glob(f"{phase}-*.active.json")):
        payload = load_optional_json(path)
        if not isinstance(payload, dict):
            continue
        if payload.get("phase") != phase:
            continue
        objective_id = str(payload.get("objective_id") or "").strip()
        if not objective_id:
            continue
        task_ids = {
            str(task_id).strip()
            for task_id in payload.get("task_ids", [])
            if str(task_id).strip()
        }
        task_ids_by_objective[objective_id] = task_ids
    return task_ids_by_objective


def active_phase_tasks(run_dir: Path, phase: str) -> list[dict[str, Any]]:
    tasks_dir = run_dir / "tasks"
    if not tasks_dir.exists():
        return []
    active_task_ids = active_task_ids_by_objective_for_phase(run_dir, phase)
    tasks: list[dict[str, Any]] = []
    for path in sorted(tasks_dir.glob("*.json")):
        task = read_json(path)
        if task.get("phase") != phase:
            continue
        objective_id = str(task.get("objective_id") or "").strip()
        task_id = str(task.get("task_id") or "").strip()
        if objective_id in active_task_ids and task_id not in active_task_ids[objective_id]:
            continue
        tasks.append(task)
    return tasks
