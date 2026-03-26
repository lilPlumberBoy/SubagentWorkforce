from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from .contract_authority import (
    authoritative_capability_for_contract_kind,
    contract_kind_for_reference,
    is_frontend_consumption_contract_path as is_frontend_consumption_contract_reference,
)
from .filesystem import read_json, read_text, write_json, write_text
from .input_lineage import build_task_input_source_metadata
from .observability import planning_compaction_profile, prompt_metrics
from .objective_roots import (
    app_shared_workspace_ownership,
    capability_owned_shared_workspace_paths,
    capability_owned_path_hints,
    capability_shared_asset_hints,
    find_objective_app_root,
    find_objective_root,
)
from .output_descriptors import descriptor_summary, normalize_output_descriptors
from .planner import assert_active_phase
from .worktree_manager import integration_workspace_path

PHASE_SEQUENCE = ["discovery", "design", "mvp-build", "polish"]


def phase_overlay_path(project_root: Path, phase: str, prompt_kind: str) -> Path:
    return project_root / "orchestrator" / "phase-overlays" / phase / f"{prompt_kind}.md"


def relative_path_or_none(base: Path, candidate: Path | None) -> str | None:
    if candidate is None:
        return None
    try:
        return str(candidate.resolve().relative_to(base.resolve()))
    except ValueError:
        try:
            return str(candidate.relative_to(base))
        except ValueError:
            return str(candidate)


def resolve_workspace_input_path(
    project_root: Path,
    run_id: str,
    input_path: str,
    *,
    extra_roots: list[Path] | None = None,
) -> Path | None:
    candidate_path = Path(str(input_path).strip())
    if not str(candidate_path):
        return None
    if candidate_path.is_absolute():
        return candidate_path if candidate_path.exists() else None
    search_roots: list[Path] = [project_root]
    integration_workspace = integration_workspace_path(project_root, run_id)
    if integration_workspace.exists():
        search_roots.append(integration_workspace)
    search_roots.extend(extra_roots or [])
    seen: set[str] = set()
    for root in search_roots:
        resolved_root = root.resolve()
        key = str(resolved_root)
        if key in seen:
            continue
        seen.add(key)
        candidate = (resolved_root / candidate_path).resolve()
        if candidate.exists():
            return candidate
    return None


def render_prompt(
    project_root: Path,
    run_id: str,
    task_path: Path,
    *,
    working_directory: Path | None = None,
    sandbox_mode: str | None = None,
    task_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = dict(task_payload) if task_payload is not None else read_json(task_path)
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
    objective_root = find_objective_root(project_root, objective_id)
    role_path = objective_root / "approved" / f"{role_name}.md"
    if role_path.exists():
        add(role_path)
    else:
        add(objective_root / "charter.md")

    phase_path = phase_overlay_path(project_root, task["phase"], "task-execution")
    add(phase_path)

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
    runtime_context = build_task_runtime_context(
        project_root,
        run_id,
        task,
        files_loaded,
        metadata["prompt_path"],
        role_kind,
        working_directory=working_directory,
        sandbox_mode=sandbox_mode,
    )
    rendered_task_payload = dict(task)
    if working_directory is not None:
        rendered_task_payload["working_directory"] = str(working_directory)
    if sandbox_mode is not None:
        rendered_task_payload["sandbox_mode"] = sandbox_mode
    rendered_task = json.dumps(compact_task_assignment(rendered_task_payload), indent=2, sort_keys=True)
    resolved_inputs = resolve_task_inputs(project_root, run_id, task, runtime_context)
    input_source_metadata = build_task_input_source_metadata(project_root, run_id, task)
    metadata["resolved_input_refs"] = sorted(resolved_inputs)
    metadata["input_source_refs"] = sorted(input_source_metadata)
    renderable_resolved_inputs = compact_resolved_inputs_for_prompt(resolved_inputs)
    renderable_input_source_metadata = compact_resolved_inputs_for_prompt(input_source_metadata)
    dependency_preview_section = build_dependency_preview_section(resolved_inputs)
    task_contract_section = build_task_contract_section(task)
    prompt_text = "\n\n".join(
        parts
        + [
            task_contract_section,
            "# Runtime Context\n\n```json\n" + json.dumps(runtime_context, indent=2, sort_keys=True) + "\n```",
            "# Resolved Inputs\n\n```json\n" + json.dumps(renderable_resolved_inputs, indent=2, sort_keys=True) + "\n```",
            "# Input Source Metadata\n\n```json\n"
            + json.dumps(renderable_input_source_metadata, indent=2, sort_keys=True)
            + "\n```",
            dependency_preview_section,
            f"# Task Assignment\n\n```json\n{rendered_task}\n```",
        ]
    )
    write_text(prompt_path, prompt_text)
    write_json(log_path, metadata)
    return metadata


def render_objective_planning_prompt(
    project_root: Path,
    run_id: str,
    objective_id: str,
    *,
    ignore_existing_phase_tasks: bool = False,
    repair_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase_plan = read_json(run_dir / "phase-plan.json")
    phase = phase_plan["current_phase"]
    files_loaded: list[str] = []
    parts: list[str] = []
    minimal_repair_parts = bool(repair_context and repair_context.get("compact_retry_used"))

    def add(path: Path) -> None:
        parts.append(read_text(path))
        files_loaded.append(str(path.relative_to(project_root)))

    if not minimal_repair_parts:
        add(project_root / "orchestrator" / "roles" / "base" / "company.md")
    add(project_root / "orchestrator" / "roles" / "base" / "manager.md")
    add(project_root / "orchestrator" / "roles" / "base" / "objective-manager.md")

    objective_root = find_objective_root(project_root, objective_id)
    add(objective_root / "charter.md")
    objective_manager_path = objective_root / "approved" / "objective-manager.md"
    if objective_manager_path.exists() and not minimal_repair_parts:
        add(objective_manager_path)

    add(phase_overlay_path(project_root, phase, "objective-planning"))

    compaction = repair_prompt_compaction_profile(repair_context) or planning_compaction_profile(project_root, run_id, phase)
    planning_payload = build_planning_prompt_payload(
        project_root,
        run_id,
        objective_id,
        compaction=compaction,
        ignore_existing_phase_tasks=ignore_existing_phase_tasks,
        repair_context=repair_context,
    )
    runtime_context = build_planning_runtime_context(
        objective_id=objective_id,
        phase=phase,
        team=planning_payload["team"],
        files_loaded=files_loaded,
    )
    prompt_text = build_planning_prompt_text(
        parts,
        runtime_context,
        planning_payload,
        repair_context=repair_context,
    )
    final_compaction = maybe_escalate_planning_compaction(
        project_root,
        run_id,
        phase,
        prompt_text,
        current=compaction,
    )
    if final_compaction["level"] != compaction["level"]:
        planning_payload = build_planning_prompt_payload(
            project_root,
            run_id,
            objective_id,
            compaction=final_compaction,
            ignore_existing_phase_tasks=ignore_existing_phase_tasks,
            repair_context=repair_context,
        )
        runtime_context = build_planning_runtime_context(
            objective_id=objective_id,
            phase=phase,
            team=planning_payload["team"],
            files_loaded=files_loaded,
        )
        prompt_text = build_planning_prompt_text(
            parts,
            runtime_context,
            planning_payload,
            repair_context=repair_context,
        )
    prompt_path = run_dir / "manager-plans" / f"{phase}-{objective_id}.prompt.md"
    log_path = run_dir / "manager-plans" / f"{phase}-{objective_id}.prompt.json"
    write_text(prompt_path, prompt_text)
    prompt_stats = prompt_metrics(prompt_text)
    metadata = {
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "files_loaded": files_loaded,
        "prompt_path": str(prompt_path.relative_to(project_root)),
        "compaction_profile": final_compaction["level"],
        "compaction_reason": final_compaction["reason"],
        "prompt_char_count": prompt_stats["prompt_char_count"],
        "prompt_line_count": prompt_stats["prompt_line_count"],
    }
    write_json(log_path, metadata)
    return metadata


def render_capability_planning_prompt(
    project_root: Path,
    run_id: str,
    objective_id: str,
    capability: str,
    objective_outline: dict[str, Any],
    *,
    ignore_existing_phase_tasks: bool = False,
    repair_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase = read_json(run_dir / "phase-plan.json")["current_phase"]
    files_loaded: list[str] = []
    parts: list[str] = []
    minimal_repair_parts = bool(repair_context and repair_context.get("compact_retry_used"))

    def add(path: Path) -> None:
        parts.append(read_text(path))
        files_loaded.append(str(path.relative_to(project_root)))

    if not minimal_repair_parts:
        add(project_root / "orchestrator" / "roles" / "base" / "company.md")
    add(project_root / "orchestrator" / "roles" / "base" / "manager.md")
    add(project_root / "orchestrator" / "roles" / "base" / "capability-manager.md")

    capability_path = project_root / "orchestrator" / "roles" / "capabilities" / f"{capability}.md"
    if capability != "general" and capability_path.exists() and not minimal_repair_parts:
        add(capability_path)

    objective_root = find_objective_root(project_root, objective_id)
    add(objective_root / "charter.md")
    lane = next(item for item in objective_outline["capability_lanes"] if item["capability"] == capability)
    role_name = lane["assigned_manager_role"].split(".")[-1]
    role_path = objective_root / "approved" / f"{role_name}.md"
    if role_path.exists() and not minimal_repair_parts:
        add(role_path)

    add(phase_overlay_path(project_root, phase, "capability-planning"))

    compaction = repair_prompt_compaction_profile(repair_context) or planning_compaction_profile(project_root, run_id, phase)
    planning_payload = build_capability_prompt_payload(
        project_root,
        run_id,
        objective_id,
        capability,
        objective_outline,
        compaction=compaction,
        ignore_existing_phase_tasks=ignore_existing_phase_tasks,
        repair_context=repair_context,
    )
    runtime_context = build_capability_planning_runtime_context(
        objective_id=objective_id,
        phase=phase,
        capability=capability,
        lane=lane,
        team=planning_payload["team"],
        files_loaded=files_loaded,
    )
    prompt_text = build_capability_prompt_text(
        parts,
        runtime_context,
        planning_payload,
        repair_context=repair_context,
    )
    final_compaction = maybe_escalate_planning_compaction(
        project_root,
        run_id,
        phase,
        prompt_text,
        current=compaction,
    )
    if final_compaction["level"] != compaction["level"]:
        planning_payload = build_capability_prompt_payload(
            project_root,
            run_id,
            objective_id,
            capability,
            objective_outline,
            compaction=final_compaction,
            ignore_existing_phase_tasks=ignore_existing_phase_tasks,
            repair_context=repair_context,
        )
        runtime_context = build_capability_planning_runtime_context(
            objective_id=objective_id,
            phase=phase,
            capability=capability,
            lane=lane,
            team=planning_payload["team"],
            files_loaded=files_loaded,
        )
        prompt_text = build_capability_prompt_text(
            parts,
            runtime_context,
            planning_payload,
            repair_context=repair_context,
        )
    prompt_path = run_dir / "manager-plans" / f"{phase}-{objective_id}-{capability}.prompt.md"
    log_path = run_dir / "manager-plans" / f"{phase}-{objective_id}-{capability}.prompt.json"
    write_text(prompt_path, prompt_text)
    prompt_stats = prompt_metrics(prompt_text)
    metadata = {
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "capability": capability,
        "files_loaded": files_loaded,
        "prompt_path": str(prompt_path.relative_to(project_root)),
        "compaction_profile": final_compaction["level"],
        "compaction_reason": final_compaction["reason"],
        "prompt_char_count": prompt_stats["prompt_char_count"],
        "prompt_line_count": prompt_stats["prompt_line_count"],
    }
    write_json(log_path, metadata)
    return metadata


def build_task_runtime_context(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    files_loaded: list[str],
    prompt_path: str,
    role_kind: str,
    *,
    working_directory: Path | None = None,
    sandbox_mode: str | None = None,
) -> dict[str, Any]:
    objective_payload = build_planning_payload(project_root, run_id, task["objective_id"])
    manager_role = task.get("manager_role")
    return {
        "available_roles": [
            f"objectives.{task['objective_id']}.{role['role_id']}" for role in objective_payload["team"]["roles"]
        ],
        "assigned_role": task["assigned_role"],
        # Keep the planning/runtime vocabulary aligned so generated inputs can
        # reference the manager role consistently across planning and execution.
        "assigned_manager_role": manager_role,
        "acceptance_role": task.get("acceptance_role"),
        "capability": task.get("capability"),
        "execution_mode": task.get("execution_mode"),
        "manager_role": manager_role,
        "objective_id": task["objective_id"],
        "parallel_policy": task.get("parallel_policy"),
        "phase": task["phase"],
        "planning_schema": "objective-plan.v1",
        "prompt_layers_loaded": files_loaded,
        "prompt_log_path": prompt_path,
        "role_kind": role_kind,
        "run_id": run_id,
        "sandbox_mode": sandbox_mode if sandbox_mode is not None else task.get("sandbox_mode"),
        "task_id": task.get("task_id"),
        "working_directory": str(working_directory) if working_directory is not None else task.get("working_directory"),
        "additional_directories": list(task.get("additional_directories", [])),
        "owned_paths": list(task.get("owned_paths", [])),
        "writes_existing_paths": list(task.get("writes_existing_paths", [])),
        "expected_outputs": compact_output_descriptors(list(task.get("expected_outputs", [])), limit=6),
        "workspace_hints": objective_payload.get("workspace_hints"),
        "worker_roles": [
            f"objectives.{task['objective_id']}.{role['role_id']}"
            for role in objective_payload["team"]["roles"]
            if team_role_kind(role) == "worker"
        ],
    }


def compact_task_assignment(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": task["schema"],
        "run_id": task["run_id"],
        "phase": task["phase"],
        "objective_id": task["objective_id"],
        "task_id": task["task_id"],
        "objective": task.get("objective"),
        "inputs": list(task.get("inputs", []))[:6],
        "depends_on": list(task.get("depends_on", [])),
        "handoff_dependencies": list(task.get("handoff_dependencies", [])),
        "shared_asset_ids": list(task.get("shared_asset_ids", []))[:6],
        "done_when": compact_text_list([str(item) for item in task.get("done_when", [])], limit=3, max_length=120),
        "validation": compact_validation_steps(task.get("validation", []), limit=3),
        "collaboration_rules": compact_text_list(
            [str(item) for item in task.get("collaboration_rules", [])],
            limit=3,
            max_length=120,
        ),
    }


def build_planning_runtime_context(
    *, objective_id: str, phase: str, team: dict[str, Any], files_loaded: list[str]
) -> dict[str, Any]:
    return {
        "prompt_layers_loaded": files_loaded,
        "planning_schema": "objective-outline.v1",
        "objective_id": objective_id,
        "phase": phase,
        "available_roles": [f"objectives.{objective_id}.{role['role_id']}" for role in team["roles"]],
        "worker_roles": [f"objectives.{objective_id}.{role['role_id']}" for role in team["roles"] if team_role_kind(role) == "worker"],
    }


def build_capability_planning_runtime_context(
    *,
    objective_id: str,
    phase: str,
    capability: str,
    lane: dict[str, Any],
    team: dict[str, Any],
    files_loaded: list[str],
) -> dict[str, Any]:
    return {
        "prompt_layers_loaded": files_loaded,
        "planning_schema": "capability-plan.v1",
        "objective_id": objective_id,
        "phase": phase,
        "capability": capability,
        "assigned_manager_role": lane["assigned_manager_role"],
        "available_roles": [f"objectives.{objective_id}.{role['role_id']}" for role in team["roles"]],
        "worker_roles": [
            f"objectives.{objective_id}.{role['role_id']}"
            for role in team["roles"]
            if team_role_kind(role) == "worker" and role.get("capability", "general") in {capability, "general"}
        ],
    }


def team_role_kind(role: dict[str, Any]) -> str:
    return str(role.get("role_kind") or role.get("role_type") or "worker")


def build_planning_payload(
    project_root: Path,
    run_id: str,
    objective_id: str,
    *,
    ignore_existing_phase_tasks: bool = False,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase_plan = read_json(run_dir / "phase-plan.json")
    phase = phase_plan["current_phase"]
    objective_map = read_json(run_dir / "objective-map.json")
    team_registry = read_json(run_dir / "team-registry.json")
    objective = next(item for item in objective_map["objectives"] if item["objective_id"] == objective_id)
    team = next(item for item in team_registry["teams"] if item["objective_id"] == objective_id)
    objective_root = find_objective_root(project_root, objective_id)
    app_root = find_objective_app_root(project_root, objective_id)
    objective_capabilities = [
        str(value).strip()
        for value in objective.get("capabilities", [])
        if isinstance(value, str) and str(value).strip()
    ]
    primary_capability = objective_capabilities[0] if len(objective_capabilities) == 1 else "general"
    existing_phase_tasks = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        task = read_json(path)
        if task["phase"] == phase and task["objective_id"] == objective_id:
            if ignore_existing_phase_tasks:
                continue
            existing_phase_tasks.append(task)
    all_prior_phase_reports = collect_prior_phase_reports(run_dir, objective_id, phase)
    prior_phase_reports = select_detailed_prior_phase_reports(all_prior_phase_reports, phase)
    prior_phase_artifacts = collect_prior_phase_artifacts(project_root, all_prior_phase_reports)
    canonical_contracts = collect_canonical_contracts(project_root, run_dir, objective_id, phase)
    prior_phase_artifacts = filter_noncanonical_contract_artifacts(
        prior_phase_artifacts,
        canonical_contracts=canonical_contracts,
        allow_consumer_contracts=primary_capability == "frontend",
    )
    related_prior_phase_reports = collect_related_app_prior_phase_reports(
        project_root,
        run_dir,
        objective_id,
        "general",
        phase,
    )
    related_prior_phase_artifacts = collect_prior_phase_artifacts(project_root, related_prior_phase_reports)
    related_prior_phase_artifacts = filter_noncanonical_contract_artifacts(
        related_prior_phase_artifacts,
        canonical_contracts=canonical_contracts,
        allow_consumer_contracts=False,
    )
    related_prior_phase_reports, related_prior_phase_artifacts = filter_backend_mvp_build_related_inputs(
        objective,
        capability="general",
        phase=phase,
        related_reports=related_prior_phase_reports,
        related_artifacts=related_prior_phase_artifacts,
    )
    prior_phase_phase_reports = collect_completed_phase_reports(run_dir, phase_plan, phase)
    return {
        "goal_markdown": read_text(run_dir / "goal.md"),
        "objective": objective,
        "team": team,
        "workspace_hints": {
            "objective_role_root": relative_path_or_none(project_root, objective_root),
            "objective_app_root": relative_path_or_none(project_root, app_root),
        },
        "shared_workspace_ownership": app_shared_workspace_ownership(project_root, app_root),
        "objective_contract_hints": build_objective_contract_hints(
            project_root,
            run_id,
            objective_id,
            objective_capabilities,
            phase=phase,
        ),
        "existing_phase_tasks": existing_phase_tasks,
        "existing_phase_tasks_by_id": {
            task["task_id"]: task
            for task in existing_phase_tasks
        },
        "prior_phase_reports": prior_phase_reports,
        "prior_phase_artifacts": prior_phase_artifacts,
        "related_prior_phase_reports": related_prior_phase_reports,
        "related_prior_phase_artifacts": related_prior_phase_artifacts,
        "canonical_contracts": canonical_contracts,
        "approved_inputs_catalog": {
            "report_paths": [item["report_path"] for item in all_prior_phase_reports],
            "artifact_paths": [item["path"] for item in prior_phase_artifacts],
            "phase_report_paths": prior_phase_phase_reports,
        },
    }


def build_capability_planning_payload(
    project_root: Path,
    run_id: str,
    objective_id: str,
    capability: str,
    objective_outline: dict[str, Any],
    *,
    ignore_existing_phase_tasks: bool = False,
) -> dict[str, Any]:
    planning_payload = build_planning_payload(
        project_root,
        run_id,
        objective_id,
        ignore_existing_phase_tasks=ignore_existing_phase_tasks,
    )
    run_dir = project_root / "runs" / run_id
    phase = str(objective_outline.get("phase", planning_payload["objective"].get("phase", "discovery")))
    lane = next(item for item in objective_outline["capability_lanes"] if item["capability"] == capability)
    existing_capability_tasks = [
        task
        for task in planning_payload["existing_phase_tasks"]
        if task.get("capability") in {capability, None}
    ]
    required_outbound_handoffs = [
        edge for edge in objective_outline.get("collaboration_edges", []) if edge["from_capability"] == capability
    ]
    required_inbound_handoffs = [
        edge
        for edge in objective_outline.get("collaboration_edges", [])
        if edge["to_capability"] == capability and edge["from_capability"] != capability
    ]
    relevant_collaboration_edges = [
        edge
        for edge in objective_outline.get("collaboration_edges", [])
        if edge["from_capability"] == capability or edge["to_capability"] == capability
    ]
    related_prior_phase_reports = collect_related_app_prior_phase_reports(
        project_root,
        run_dir,
        objective_id,
        capability,
        phase,
    )
    related_prior_phase_artifacts = collect_prior_phase_artifacts(project_root, related_prior_phase_reports)
    related_prior_phase_artifacts = filter_noncanonical_contract_artifacts(
        related_prior_phase_artifacts,
        canonical_contracts=planning_payload["canonical_contracts"],
        allow_consumer_contracts=False,
    )
    related_prior_phase_reports, related_prior_phase_artifacts = filter_backend_mvp_build_related_inputs(
        planning_payload["objective"],
        capability=capability,
        phase=phase,
        related_reports=related_prior_phase_reports,
        related_artifacts=related_prior_phase_artifacts,
    )
    enriched_outline = dict(objective_outline)
    enriched_outline["relevant_collaboration_edges"] = relevant_collaboration_edges
    app_root = find_objective_app_root(project_root, objective_id)
    objective_root = find_objective_root(project_root, objective_id)
    owned_path_hints = capability_owned_path_hints(
        project_root,
        objective_id,
        capability,
        phase=phase,
    )
    if phase == "mvp-build" and capability == "middleware":
        objective_root_rel = str(objective_root.relative_to(project_root))
        owned_path_hints = [
            f"{objective_root_rel}/build/**",
            f"{objective_root_rel}/artifacts/**",
            f"{objective_root_rel}/**",
        ]
    release_repair_inputs = build_release_repair_inputs(
        project_root,
        run_id,
        objective_id,
        capability=capability,
        phase=phase,
    )
    return {
        "goal_markdown": planning_payload["goal_markdown"],
        "objective": planning_payload["objective"],
        "team": planning_payload["team"],
        "workspace_hints": planning_payload["workspace_hints"],
        "shared_workspace_ownership": planning_payload["shared_workspace_ownership"],
        "objective_contract_hints": planning_payload["objective_contract_hints"],
        "objective_outline": enriched_outline,
        "capability_lane": lane,
        "allowed_final_outputs_exact": normalize_output_descriptors(list(lane.get("expected_outputs", []))),
        "existing_required_output_paths_exact": existing_required_output_paths_for_prompt(
            project_root,
            run_id,
            lane.get("expected_outputs", []),
        ),
        "existing_capability_tasks": existing_capability_tasks,
        "existing_capability_tasks_by_id": {
            task["task_id"]: task
            for task in existing_capability_tasks
        },
        "capability_scope_hints": {
            "owned_path_hints": owned_path_hints,
            "shared_asset_hints": capability_shared_asset_hints(objective_id, capability),
            "shared_root_owned_paths": capability_owned_shared_workspace_paths(project_root, app_root, capability),
        },
        "required_outbound_handoffs": annotate_handoff_deliverable_refs(required_outbound_handoffs, field_name="required_outbound_handoffs"),
        "required_outbound_handoffs_exact": annotate_handoff_deliverable_refs(
            required_outbound_handoffs,
            field_name="required_outbound_handoffs",
        ),
        "required_inbound_handoffs": annotate_handoff_deliverable_refs(required_inbound_handoffs, field_name="required_inbound_handoffs"),
        "prior_phase_reports": planning_payload["prior_phase_reports"],
        "prior_phase_artifacts": planning_payload["prior_phase_artifacts"],
        "related_prior_phase_reports": related_prior_phase_reports,
        "related_prior_phase_artifacts": related_prior_phase_artifacts,
        "canonical_contracts": planning_payload["canonical_contracts"],
        "approved_inputs_catalog": planning_payload["approved_inputs_catalog"],
        "release_repair_inputs": release_repair_inputs,
        "release_repair_input_refs": release_repair_input_refs(release_repair_inputs),
    }


def build_planning_prompt_payload(
    project_root: Path,
    run_id: str,
    objective_id: str,
    *,
    compaction: dict[str, Any] | None = None,
    ignore_existing_phase_tasks: bool = False,
    repair_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    planning_payload = build_planning_payload(
        project_root,
        run_id,
        objective_id,
        ignore_existing_phase_tasks=ignore_existing_phase_tasks,
    )
    limits = (compaction or planning_compaction_profile(project_root, run_id, planning_payload["objective"].get("phase", "discovery"))).get("limits", {})
    compacted = dict(planning_payload)
    selected_related_reports = select_related_prior_phase_reports_for_prompt(
        planning_payload["related_prior_phase_reports"],
        limit=int(limits.get("prior_reports", 6)),
    )
    compacted["objective"] = compact_objective_record(planning_payload["objective"])
    compacted["team"] = compact_team_record(planning_payload["team"])
    compacted["goal_context"] = compact_goal_context(
        planning_payload["goal_markdown"],
        objective_id=objective_id,
        objective_title=str(planning_payload["objective"].get("title", "")),
        objective_summary=str(planning_payload["objective"].get("summary", "")),
        objective_detail_limit=int(limits.get("objective_details", 6)),
        section_max_length=int(limits.get("section_max_length", 420)),
        detail_max_length=int(limits.get("detail_max_length", 520)),
    )
    compacted.pop("goal_markdown", None)
    compacted["existing_phase_tasks"] = summarize_existing_phase_tasks(
        planning_payload["existing_phase_tasks"],
        limit=int(limits.get("existing_tasks", 8)),
    )
    compacted["existing_phase_tasks_by_id"] = {
        task["task_id"]: summary
        for task, summary in zip(
            planning_payload["existing_phase_tasks"][: int(limits.get("existing_tasks", 8))],
            compacted["existing_phase_tasks"],
        )
    }
    compacted["prior_phase_reports"] = planning_payload["prior_phase_reports"][: int(limits.get("prior_reports", 6))]
    compacted["prior_phase_artifacts"] = planning_payload["prior_phase_artifacts"][: int(limits.get("prior_artifacts", 8))]
    compacted["related_prior_phase_reports"] = selected_related_reports
    compacted["related_prior_phase_artifacts"] = filter_noncanonical_contract_artifacts(
        collect_prior_phase_artifacts(project_root, selected_related_reports),
        canonical_contracts=planning_payload["canonical_contracts"],
        allow_consumer_contracts=False,
    )[: int(limits.get("prior_artifacts", 8))]
    compacted["canonical_contracts"] = planning_payload["canonical_contracts"]
    compacted["approved_inputs_catalog"] = {
        "report_paths": planning_payload["approved_inputs_catalog"]["report_paths"][: int(limits.get("catalog_reports", 12))],
        "artifact_paths": planning_payload["approved_inputs_catalog"]["artifact_paths"][: int(limits.get("catalog_artifacts", 12))],
        "phase_report_paths": planning_payload["approved_inputs_catalog"]["phase_report_paths"][:4],
    }
    compacted["shared_workspace_ownership"] = planning_payload["shared_workspace_ownership"]
    if is_compact_release_repair_context(repair_context):
        compacted = apply_release_repair_payload_compaction(compacted, repair_context=repair_context)
    return compacted


def build_capability_prompt_payload(
    project_root: Path,
    run_id: str,
    objective_id: str,
    capability: str,
    objective_outline: dict[str, Any],
    *,
    compaction: dict[str, Any] | None = None,
    ignore_existing_phase_tasks: bool = False,
    repair_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_capability_planning_payload(
        project_root,
        run_id,
        objective_id,
        capability,
        objective_outline,
        ignore_existing_phase_tasks=ignore_existing_phase_tasks,
    )
    limits = (compaction or planning_compaction_profile(project_root, run_id, payload["objective_outline"]["phase"] if "phase" in payload["objective_outline"] else "discovery")).get("limits", {})
    compacted = dict(payload)
    selected_related_reports = select_related_prior_phase_reports_for_prompt(
        payload["related_prior_phase_reports"],
        limit=int(limits.get("prior_reports", 6)),
    )
    compacted["objective"] = compact_objective_record(payload["objective"])
    compacted["team"] = compact_team_record(payload["team"])
    compacted["goal_context"] = compact_goal_context(
        payload["goal_markdown"],
        objective_id=objective_id,
        objective_title=str(payload["objective"].get("title", "")),
        objective_summary=str(payload["objective"].get("summary", "")),
        objective_detail_limit=int(limits.get("objective_details", 6)),
        section_max_length=int(limits.get("section_max_length", 420)),
        detail_max_length=int(limits.get("detail_max_length", 520)),
    )
    compacted.pop("goal_markdown", None)
    compacted["existing_capability_tasks"] = summarize_existing_phase_tasks(
        payload["existing_capability_tasks"],
        limit=int(limits.get("existing_tasks", 8)),
    )
    compacted["existing_capability_tasks_by_id"] = {
        task["task_id"]: summary
        for task, summary in zip(
            payload["existing_capability_tasks"][: int(limits.get("existing_tasks", 8))],
            compacted["existing_capability_tasks"],
        )
    }
    compacted["objective_outline"] = compact_objective_outline_for_prompt(
        objective_outline,
        capability=capability,
        edge_limit=int(limits.get("outline_edges", 8)),
        summary_max_length=int(limits.get("outline_summary_max_length", 260)),
        dependency_note_limit=int(limits.get("dependency_note_limit", 5)),
        dependency_note_max_length=int(limits.get("dependency_note_max_length", 160)),
    )
    compacted["capability_lane"] = compact_capability_lane(payload["capability_lane"])
    compacted["allowed_final_outputs_exact"] = payload["allowed_final_outputs_exact"]
    compacted["existing_required_output_paths_exact"] = payload["existing_required_output_paths_exact"]
    compacted["capability_scope_hints"] = {
        "owned_path_hints": list(payload["capability_scope_hints"]["owned_path_hints"])[:3],
        "shared_asset_hints": list(payload["capability_scope_hints"]["shared_asset_hints"])[:4],
        "shared_root_owned_paths": list(payload["capability_scope_hints"]["shared_root_owned_paths"])[:6],
    }
    compacted["required_outbound_handoffs"] = compact_collaboration_edges(
        payload["required_outbound_handoffs"],
        limit=int(limits.get("outline_edges", 8)),
    )
    compacted["required_outbound_handoffs_exact"] = payload["required_outbound_handoffs_exact"]
    compacted["required_inbound_handoffs"] = compact_collaboration_edges(
        payload["required_inbound_handoffs"],
        limit=int(limits.get("outline_edges", 8)),
    )
    compacted["prior_phase_reports"] = payload["prior_phase_reports"][: int(limits.get("prior_reports", 6))]
    compacted["prior_phase_artifacts"] = payload["prior_phase_artifacts"][: int(limits.get("prior_artifacts", 8))]
    compacted["related_prior_phase_reports"] = selected_related_reports
    compacted["related_prior_phase_artifacts"] = filter_noncanonical_contract_artifacts(
        collect_prior_phase_artifacts(project_root, selected_related_reports),
        canonical_contracts=payload["canonical_contracts"],
        allow_consumer_contracts=False,
    )[: int(limits.get("prior_artifacts", 8))]
    compacted["canonical_contracts"] = payload["canonical_contracts"]
    compacted["approved_inputs_catalog"] = {
        "report_paths": payload["approved_inputs_catalog"]["report_paths"][: int(limits.get("catalog_reports", 12))],
        "artifact_paths": payload["approved_inputs_catalog"]["artifact_paths"][: int(limits.get("catalog_artifacts", 12))],
        "phase_report_paths": payload["approved_inputs_catalog"]["phase_report_paths"][:4],
    }
    compacted["shared_workspace_ownership"] = payload["shared_workspace_ownership"]
    compacted["objective_contract_hints"] = payload["objective_contract_hints"]
    compacted["release_repair_inputs"] = payload.get("release_repair_inputs", {})
    compacted["release_repair_input_refs"] = payload.get("release_repair_input_refs", [])
    if is_compact_release_repair_context(repair_context):
        compacted = apply_release_repair_payload_compaction(
            compacted,
            repair_context=repair_context,
            capability=capability,
        )
    return compacted


def resolve_task_inputs(
    project_root: Path, run_id: str, task: dict[str, Any], runtime_context: dict[str, Any]
) -> dict[str, Any]:
    planning_payload = build_task_planning_payload(project_root, run_id, task)
    resolved: dict[str, Any] = {}
    for input_ref in task.get("inputs", []):
        resolved[input_ref] = resolve_input_reference(
            project_root,
            run_id,
            task,
            input_ref,
            runtime_context=runtime_context,
            planning_payload=planning_payload,
        )
    handoff_packages = build_task_handoff_packages(project_root, run_id, task)
    if handoff_packages:
        resolved["Resolved Handoff Packages"] = handoff_packages
    return resolved


def resolve_input_reference(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    input_ref: str,
    *,
    runtime_context: dict[str, Any],
    planning_payload: dict[str, Any],
    resolution_depth: int = 0,
) -> Any:
    normalized_ref = input_ref.strip()
    normalized_lower = normalized_ref.lower()
    if normalized_ref == "Runtime Context":
        return runtime_context
    if normalized_ref == "Planning Inputs":
        return planning_payload
    if normalized_ref.startswith("Runtime Context."):
        return lookup_dotted_path(runtime_context, normalized_ref.removeprefix("Runtime Context."))
    if normalized_ref.startswith("Planning Inputs."):
        dotted_path = normalized_ref.removeprefix("Planning Inputs.")
        resolved = lookup_dotted_path(planning_payload, dotted_path)
        if isinstance(resolved, dict) and isinstance(resolved.get("missing_path"), str):
            goal_context_fallback = resolve_goal_context_dotted_ref(planning_payload, dotted_path)
            if goal_context_fallback is not None:
                return goal_context_fallback
        if (
            resolution_depth < 4
            and isinstance(resolved, str)
            and resolved.strip()
            and resolved.strip() != normalized_ref
            and resolved.strip().startswith(("Planning Inputs.", "Runtime Context.", "Output of ", "Outputs from "))
        ):
            return resolve_input_reference(
                project_root,
                run_id,
                task,
                resolved.strip(),
                runtime_context=runtime_context,
                planning_payload=planning_payload,
                resolution_depth=resolution_depth + 1,
            )
        if dotted_path.endswith(".path") and isinstance(resolved, str) and resolved.strip():
            candidate = resolve_workspace_input_path(project_root, run_id, resolved.strip())
            if candidate is not None and candidate.is_file():
                return {
                    "path": resolved.strip(),
                    "content": read_path(candidate),
                }
        return resolved
    if normalized_lower.startswith("output of "):
        task_id = normalized_ref.split(" ", 2)[2].strip()
        return resolve_task_output(project_root, run_id, task_id)
    if normalized_lower.startswith("outputs from "):
        task_id = normalized_ref.split(" ", 2)[2].strip()
        return resolve_task_output(project_root, run_id, task_id)
    candidate = resolve_workspace_input_path(project_root, run_id, normalized_ref)
    if candidate is not None and candidate.is_file():
        return read_path(candidate)
    special_resolution = resolve_natural_language_input_ref(
        project_root,
        run_id,
        input_ref=normalized_ref,
        runtime_context=runtime_context,
        planning_payload=planning_payload,
    )
    if special_resolution is not None:
        return special_resolution
    fallback_artifact = fallback_report_artifact(planning_payload, normalized_ref)
    if fallback_artifact is not None:
        return fallback_artifact
    return {"unresolved_input_ref": normalized_ref}


def preview_resolved_inputs(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    *,
    working_directory: Path | None = None,
    sandbox_mode: str | None = None,
) -> dict[str, Any]:
    role_kind = _infer_role_kind(task["assigned_role"].split(".")[-1])
    runtime_context = build_task_runtime_context(
        project_root,
        run_id,
        task,
        [],
        "",
        role_kind,
        working_directory=working_directory,
        sandbox_mode=sandbox_mode,
    )
    return resolve_task_inputs(project_root, run_id, task, runtime_context)


def compact_resolved_inputs_for_prompt(payload: Any) -> Any:
    if isinstance(payload, dict):
        compacted: dict[str, Any] = {}
        preferred_keys = [
            "status",
            "summary",
            "artifacts",
            "artifact_previews",
            "delivered_payloads",
            "open_issues",
            "task_id",
            "from_task_id",
            "source_summary",
            "schema",
            "phase",
            "objective_id",
            "run_id",
        ]
        items_by_key = list(payload.items())
        output_like_keys = [
            key
            for key, _ in items_by_key
            if key.startswith(("Output of ", "output of ", "Outputs from ", "outputs from "))
            or key == "Resolved Handoff Packages"
        ]
        prioritized_keys = list(output_like_keys)
        prioritized_keys.extend(key for key in preferred_keys if key in payload and key not in prioritized_keys)
        prioritized_keys.extend(
            key for key, _ in items_by_key if key not in prioritized_keys
        )
        selected_keys = prioritized_keys[:6]
        for key in selected_keys:
            value = payload[key]
            if key in {"report_path", "source_report_path", "prompt_log_path"}:
                continue
            if key in {"artifact_previews", "delivered_payloads"} and isinstance(value, list):
                compacted[key] = [
                    compact_preview_descriptor(item)
                    for item in value[:2]
                    if isinstance(item, dict)
                ]
                continue
            if key == "preview" and isinstance(value, str):
                compacted[key] = compact_text(value, max_length=240)
                continue
            compacted[key] = compact_resolved_inputs_for_prompt(value)
        if len(items_by_key) > 6:
            compacted["truncated_fields"] = len(items_by_key) - 6
        return compacted
    if isinstance(payload, list):
        compacted_items = [compact_resolved_inputs_for_prompt(item) for item in payload[:3]]
        if len(payload) > 3:
            compacted_items.append({"truncated_items": len(payload) - 3})
        return compacted_items
    if isinstance(payload, str):
        return compact_text(payload, max_length=260)
    return payload


def compact_preview_descriptor(payload: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {}
    for key in ("path", "status", "source"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            compacted[key] = value
    if payload.get("preview") not in (None, "", []):
        compacted["preview_available"] = True
    return compacted


def resolve_task_output(project_root: Path, run_id: str, task_id: str) -> Any:
    run_dir = project_root / "runs" / run_id
    report_path = run_dir / "reports" / f"{task_id}.json"
    if report_path.exists():
        report = read_json(report_path)
        compacted = compact_task_output_report(report)
        artifact_previews: list[dict[str, Any]] = []
        for artifact in report.get("artifacts", [])[:4]:
            artifact_path = artifact.get("path")
            if not isinstance(artifact_path, str) or not artifact_path:
                continue
            preview = build_handoff_artifact_preview(project_root, run_id, task_id, artifact_path)
            if preview is None:
                continue
            artifact_previews.append(
                {
                    "path": artifact_path,
                    "status": artifact.get("status"),
                    "preview": preview,
                }
            )
        if artifact_previews:
            compacted["artifact_previews"] = artifact_previews
        return compacted
    execution_path = run_dir / "executions" / f"{task_id}.json"
    if execution_path.exists():
        return compact_execution_output(read_json(execution_path), execution_path.relative_to(project_root))
    return {"missing_task_output": task_id}


def compact_task_output_report(report: dict[str, Any]) -> dict[str, Any]:
    compacted: dict[str, Any] = {
        "schema": report.get("schema"),
        "run_id": report.get("run_id"),
        "phase": report.get("phase"),
        "objective_id": report.get("objective_id"),
        "task_id": report.get("task_id"),
        "status": report.get("status"),
        "summary": compact_text(report.get("summary", ""), max_length=180),
        "artifacts": compact_artifacts(report.get("artifacts", []), limit=4),
        "produced_outputs": compact_output_descriptors(list(report.get("produced_outputs", [])), limit=4),
    }
    open_issues = compact_text_list(report.get("open_issues", []), limit=2, max_length=140)
    if open_issues:
        compacted["open_issues"] = open_issues
    return {key: value for key, value in compacted.items() if value not in (None, [], {})}


def build_task_handoff_packages(project_root: Path, run_id: str, task: dict[str, Any]) -> dict[str, Any]:
    handoff_ids = [value for value in task.get("handoff_dependencies", []) if isinstance(value, str)]
    if not handoff_ids:
        return {}
    run_dir = project_root / "runs" / run_id
    packages: dict[str, Any] = {}
    for handoff_id in handoff_ids:
        handoff_path = run_dir / "collaboration-plans" / f"{handoff_id}.json"
        if not handoff_path.exists():
            packages[handoff_id] = {
                "handoff_id": handoff_id,
                "status": "missing",
                "status_reason": "The collaboration handoff file is missing.",
            }
            continue
        handoff = read_json(handoff_path)
        package: dict[str, Any] = {
            "handoff_id": handoff_id,
            "status": handoff.get("status"),
            "status_reason": handoff.get("status_reason"),
            "from_task_id": handoff.get("from_task_id"),
            "deliverables": compact_output_descriptors(list(handoff.get("deliverables", [])), limit=4),
            "satisfied_by_task_ids": list(handoff.get("satisfied_by_task_ids", [])),
            "source_report_path": None,
            "source_summary": None,
            "source_artifacts": [],
            "delivered_payloads": [],
        }
        source_task_id = handoff.get("from_task_id")
        if not isinstance(source_task_id, str) or not source_task_id:
            packages[handoff_id] = package
            continue
        report_path = run_dir / "reports" / f"{source_task_id}.json"
        if not report_path.exists():
            packages[handoff_id] = package
            continue
        report = read_json(report_path)
        package["source_report_path"] = str(report_path.relative_to(project_root))
        package["source_summary"] = compact_text(report.get("summary", ""), max_length=220)
        package["source_artifacts"] = compact_artifacts(report.get("artifacts", []), limit=6)
        package["produced_outputs"] = compact_output_descriptors(list(report.get("produced_outputs", [])), limit=4)
        delivered_payloads: list[dict[str, Any]] = []
        for artifact in report.get("artifacts", [])[:8]:
            artifact_path = artifact.get("path")
            if not isinstance(artifact_path, str) or not artifact_path:
                continue
            preview = build_handoff_artifact_preview(
                project_root,
                run_id,
                source_task_id,
                artifact_path,
            )
            if preview is None:
                continue
            delivered_payloads.append(
                {
                    "path": artifact_path,
                    "status": artifact.get("status"),
                    "preview": preview,
                }
            )
        package["delivered_payloads"] = delivered_payloads
        packages[handoff_id] = package
    return packages


def build_dependency_preview_section(resolved_inputs: dict[str, Any]) -> str:
    sections: list[str] = []
    for input_ref, payload in list(resolved_inputs.items())[:4]:
        previews = collect_input_artifact_previews(payload)
        if not previews:
            continue
        lines = [f"## {input_ref}"]
        for preview in previews[:1]:
            lines.append(f"- `{preview['path']}`")
            if preview.get("source"):
                lines.append(f"  source: `{preview['source']}`")
            lines.append(f"  preview: {compact_text(str(preview['preview']), max_length=240)}")
        sections.append("\n".join(lines))
    if not sections:
        return "# Dependency Artifact Previews\n\nNone"
    return "# Dependency Artifact Previews\n\n" + "\n\n".join(sections)


def collect_input_artifact_previews(payload: Any) -> list[dict[str, str]]:
    previews: list[dict[str, str]] = []
    if isinstance(payload, dict):
        for item in payload.get("artifact_previews", []):
            if not isinstance(item, dict):
                continue
            preview_text = item.get("preview")
            path = item.get("path")
            if isinstance(path, str) and isinstance(preview_text, str) and preview_text.strip():
                previews.append(
                    {
                        "path": path,
                        "preview": preview_text,
                        "source": str(payload.get("task_id", "")) if payload.get("task_id") else "",
                    }
                )
        for item in payload.get("delivered_payloads", []):
            if not isinstance(item, dict):
                continue
            preview_text = item.get("preview")
            path = item.get("path")
            if isinstance(path, str) and isinstance(preview_text, str) and preview_text.strip():
                previews.append(
                    {
                        "path": path,
                        "preview": preview_text,
                        "source": str(payload.get("from_task_id", "")) if payload.get("from_task_id") else "",
                    }
                )
        for nested in payload.values():
            previews.extend(collect_input_artifact_previews(nested))
    elif isinstance(payload, list):
        for item in payload:
            previews.extend(collect_input_artifact_previews(item))
    return previews


def build_handoff_artifact_preview(
    project_root: Path,
    run_id: str,
    source_task_id: str,
    artifact_path: str,
) -> Any | None:
    candidate = resolve_report_artifact_path(project_root, run_id, source_task_id, artifact_path)
    if candidate is None or not candidate.exists():
        return None
    if candidate.suffix == ".json":
        payload = read_json(candidate)
        return compact_json_payload(payload)
    text = read_text(candidate)
    return compact_text(text, max_length=1200)


def resolve_report_artifact_path(project_root: Path, run_id: str, source_task_id: str, artifact_path: str) -> Path | None:
    path = Path(artifact_path)
    if path.is_absolute():
        return path if path.exists() else None
    search_roots: list[Path] = []
    execution_path = project_root / "runs" / run_id / "executions" / f"{source_task_id}.json"
    if execution_path.exists():
        execution = read_json(execution_path)
        workspace_path = execution.get("workspace_path")
        if isinstance(workspace_path, str) and workspace_path.strip():
            workspace = Path(workspace_path)
            if not workspace.is_absolute():
                workspace = (project_root / workspace).resolve()
            if workspace.exists():
                search_roots.append(workspace)
    return resolve_workspace_input_path(project_root, run_id, artifact_path, extra_roots=search_roots)


def compact_json_payload(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    compacted: dict[str, Any] = {}
    for key in (
        "schema",
        "run_id",
        "phase",
        "objective_id",
        "task_id",
        "handoff_id",
        "status",
        "summary",
        "deliverable_output_ids",
        "deliverables",
        "artifacts",
        "open_issues",
        "collaboration_request",
    ):
        if key not in payload:
            continue
        value = payload[key]
        if key in {"summary"}:
            compacted[key] = compact_text(str(value), max_length=220)
        elif key in {"open_issues"} and isinstance(value, list):
            compacted[key] = compact_text_list([str(item) for item in value], limit=3, max_length=160)
        elif key == "artifacts" and isinstance(value, list):
            compacted[key] = compact_artifacts(value, limit=6)
        else:
            compacted[key] = value
    return compacted or payload


def compact_execution_output(payload: Any, path: Path) -> Any:
    if not isinstance(payload, dict):
        return payload
    compacted: dict[str, Any] = {
        "task_id": payload.get("task_id"),
        "status": payload.get("status"),
        "attempt": payload.get("attempt"),
        "recovery_action": payload.get("recovery_action"),
        "parallel_execution_granted": payload.get("parallel_execution_granted"),
        "parallel_execution_requested": payload.get("parallel_execution_requested"),
        "parallel_fallback_reason": payload.get("parallel_fallback_reason"),
        "report_path": payload.get("report_path"),
        "execution_path": str(path),
        "runtime_warnings": payload.get("runtime_warnings", []),
    }
    usage = payload.get("usage")
    if isinstance(usage, dict):
        compacted["usage"] = {
            "input_tokens": usage.get("input_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }
    return {key: value for key, value in compacted.items() if value not in (None, [], {})}


def read_path(path: Path) -> Any:
    if path.suffix == ".json":
        return read_json(path)
    return read_text(path)


def fallback_report_artifact(planning_payload: dict[str, Any], artifact_path: str) -> dict[str, Any] | None:
    normalized = str(artifact_path or "").strip()
    if not normalized:
        return None
    for key in ("prior_phase_artifacts", "related_prior_phase_artifacts"):
        for item in planning_payload.get(key, []):
            if not isinstance(item, dict):
                continue
            if str(item.get("path", "")).strip() != normalized:
                continue
            fallback = {
                "path": normalized,
                "status": item.get("status"),
                "source_task_id": item.get("source_task_id"),
                "source_report_path": item.get("source_report_path"),
                "report_summary": item.get("report_summary"),
                "unlanded_artifact": True,
            }
            return {name: value for name, value in fallback.items() if value not in (None, "", [], {})}
    return None


def lookup_dotted_path(payload: Any, dotted_path: str) -> Any:
    current = payload
    if not dotted_path:
        return current
    for part in dotted_path.split("."):
        segment = part
        while segment:
            if isinstance(current, list) and segment.isdigit():
                index = int(segment)
                if index >= len(current):
                    return {"missing_path": dotted_path}
                current = current[index]
                segment = ""
                continue
            match = re.match(r"^(?P<key>[^\[]*)(?:\[(?P<index>\d+)\])(?P<rest>.*)$", segment)
            if match:
                key = match.group("key")
                if key:
                    if isinstance(current, dict) and key in current:
                        current = current[key]
                    else:
                        return {"missing_path": dotted_path}
                if not isinstance(current, list):
                    return {"missing_path": dotted_path}
                index = int(match.group("index"))
                if index >= len(current):
                    return {"missing_path": dotted_path}
                current = current[index]
                segment = match.group("rest")
                continue
            if isinstance(current, dict) and segment in current:
                current = current[segment]
                segment = ""
                continue
            return {"missing_path": dotted_path}
    return current


def build_task_planning_payload(project_root: Path, run_id: str, task: dict[str, Any]) -> dict[str, Any]:
    base_payload = build_planning_payload(project_root, run_id, task["objective_id"])
    payload = build_planning_prompt_payload(project_root, run_id, task["objective_id"])
    payload["goal_markdown"] = base_payload["goal_markdown"]
    capability = task.get("capability")
    if not capability:
        return payload
    outline_path = project_root / "runs" / run_id / "manager-plans" / f"{task['phase']}-{task['objective_id']}.outline.json"
    if not outline_path.exists():
        return payload
    objective_outline = read_json(outline_path)
    lane = next(
        (item for item in objective_outline.get("capability_lanes", []) if item.get("capability") == capability),
        None,
    )
    if lane is None:
        return payload
    capability_payload = build_capability_prompt_payload(
        project_root,
        run_id,
        task["objective_id"],
        capability,
        objective_outline,
    )
    payload.update(
        {
            "objective_outline": capability_payload["objective_outline"],
            "capability_lane": capability_payload["capability_lane"],
            "existing_capability_tasks": capability_payload["existing_capability_tasks"],
            "existing_capability_tasks_by_id": capability_payload["existing_capability_tasks_by_id"],
            "capability_scope_hints": capability_payload["capability_scope_hints"],
            "required_outbound_handoffs": capability_payload["required_outbound_handoffs"],
            "required_inbound_handoffs": capability_payload["required_inbound_handoffs"],
            "release_repair_inputs": capability_payload.get("release_repair_inputs", {}),
            "release_repair_input_refs": capability_payload.get("release_repair_input_refs", []),
        }
    )
    return payload


def resolve_natural_language_input_ref(
    project_root: Path,
    run_id: str,
    *,
    input_ref: str,
    runtime_context: dict[str, Any],
    planning_payload: dict[str, Any],
) -> Any | None:
    goal_markdown = planning_payload["goal_markdown"]
    parsed_goal = parse_goal_sections(goal_markdown)
    normalized_lower = input_ref.lower()

    if normalized_lower == "objective summary and title from planning inputs":
        return {
            "title": planning_payload["objective"]["title"],
            "summary": planning_payload["objective"]["summary"],
        }

    if normalized_lower == "team.roles and available_roles":
        return {
            "team_roles": planning_payload["team"]["roles"],
            "available_roles": runtime_context["available_roles"],
        }

    if "aggressively simple" in normalized_lower:
        return {
            "matching_lines": [
                line for line in goal_markdown.splitlines() if "simple" in line.lower() or "aggressively simple" in line.lower()
            ],
            "constraints": parsed_goal["sections"].get("Constraints"),
            "human_approval_notes": parsed_goal["sections"].get("Human Approval Notes"),
        }

    if normalized_lower.startswith("objective details:"):
        detail_name = input_ref.split(":", 1)[1].strip()
        return resolve_goal_sections(parsed_goal, [f"Objective Details -> {detail_name}"])

    if normalized_lower.startswith("objective details for "):
        detail_name = input_ref.split("for ", 1)[1].strip()
        return resolve_goal_sections(parsed_goal, [f"Objective Details -> {detail_name}"])

    if normalized_lower.startswith("goal markdown:"):
        section_text = input_ref.split(":", 1)[1].strip()
        return resolve_goal_sections(parsed_goal, split_section_reference(section_text))

    if normalized_lower.startswith("goal markdown sections:"):
        section_text = input_ref.split(":", 1)[1].strip()
        return resolve_goal_sections(parsed_goal, split_section_reference(section_text))

    if normalized_lower.startswith("planning input goal_markdown sections:"):
        section_text = input_ref.split(":", 1)[1].strip()
        return resolve_goal_sections(parsed_goal, split_section_reference(section_text))

    if normalized_lower.startswith("planning inputs goal_markdown "):
        section_text = input_ref.split("goal_markdown ", 1)[1].strip()
        return resolve_goal_sections(parsed_goal, split_section_reference(section_text))

    if normalized_lower.startswith("planning inputs design expectations"):
        return resolve_goal_sections(parsed_goal, ["Design Expectations"])

    if normalized_lower.startswith("planning inputs success criteria"):
        return resolve_goal_sections(parsed_goal, split_section_reference(input_ref.replace("Planning Inputs ", "", 1)))

    if normalized_lower.startswith("goal_markdown:"):
        section_text = input_ref.split(":", 1)[1].strip()
        if section_text.lower().endswith(" objective details"):
            detail_name = section_text[: -len(" objective details")].strip()
            return resolve_goal_sections(parsed_goal, [f"Objective Details -> {detail_name}"])
        return resolve_goal_sections(parsed_goal, split_section_reference(section_text))

    if normalized_lower.startswith("design expectations for ") or normalized_lower.startswith("design expectation"):
        return resolve_goal_sections(parsed_goal, ["Design Expectations"])

    if normalized_lower.startswith("objective details describing frontend"):
        return resolve_goal_sections(parsed_goal, ["Objective Details -> React Web Frontend"])

    if normalized_lower.startswith("objective details stating the backend"):
        return resolve_goal_sections(parsed_goal, ["Objective Details -> Backend API And Persistence"])

    if normalized_lower.startswith("goal markdown requirement that"):
        return {
            "matching_lines": match_goal_lines(goal_markdown, input_ref),
            "Success Criteria": parsed_goal["sections"].get("Success Criteria"),
            "Objective Details -> React Web Frontend": get_case_insensitive(
                parsed_goal["objective_details"], "React Web Frontend"
            ),
        }

    if normalized_lower.startswith("goal markdown success criteria"):
        return resolve_goal_sections(parsed_goal, ["Success Criteria"])

    if "in-scope and out-of-scope" in normalized_lower:
        return resolve_goal_sections(parsed_goal, ["In Scope", "Out Of Scope"])

    if "technical constraint" in normalized_lower or normalized_lower.startswith("constraint that"):
        return {
            "Constraints": parsed_goal["sections"].get("Constraints"),
            "matching_lines": match_goal_lines(goal_markdown, input_ref),
        }

    if normalized_lower.startswith("discovery expectations") or normalized_lower.startswith("known risks"):
        return resolve_goal_sections(parsed_goal, split_section_reference(input_ref))

    prior_phase_match = resolve_prior_phase_context_ref(project_root, input_ref, planning_payload)
    if prior_phase_match is not None:
        return prior_phase_match

    return None


def collect_prior_phase_reports(run_dir: Path, objective_id: str, current_phase: str) -> list[dict[str, Any]]:
    reports = []
    current_index = PHASE_SEQUENCE.index(current_phase)
    for path in sorted((run_dir / "reports").glob("*.json")):
        payload = read_json(path)
        payload_phase = payload.get("phase")
        if (
            payload.get("objective_id") != objective_id
            or payload_phase not in PHASE_SEQUENCE
            or PHASE_SEQUENCE.index(payload_phase) >= current_index
        ):
            continue
        reports.append(
            {
                "phase": payload_phase,
                "objective_id": payload["objective_id"],
                "capability": infer_report_capability(str(payload.get("agent_role", ""))),
                "task_id": payload["task_id"],
                "report_path": str(path.relative_to(run_dir.parent.parent)),
                "summary": compact_text(payload.get("summary", "")),
                "artifacts": compact_artifacts(payload.get("artifacts", [])),
                "open_issues_preview": compact_text_list(payload.get("open_issues", [])),
            }
        )
    reports.sort(key=lambda item: (PHASE_SEQUENCE.index(item["phase"]), item["task_id"]))
    return reports


def select_detailed_prior_phase_reports(reports: list[dict[str, Any]], current_phase: str) -> list[dict[str, Any]]:
    if current_phase not in PHASE_SEQUENCE:
        return reports
    current_index = PHASE_SEQUENCE.index(current_phase)
    if current_index == 0:
        return []
    immediately_previous_phase = PHASE_SEQUENCE[current_index - 1]
    selected = [report for report in reports if report["phase"] == immediately_previous_phase]
    return selected or reports


def collect_prior_phase_artifacts(project_root: Path, reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for report in reports:
        for artifact in report.get("artifacts", []):
            artifact_path = artifact.get("path")
            if not artifact_path or artifact_path.startswith("inline://") or artifact_path in seen:
                continue
            artifacts.append(
                {
                    "phase": report["phase"],
                    "objective_id": report.get("objective_id"),
                    "capability": report.get("capability"),
                    "source_task_id": report["task_id"],
                    "source_report_path": report.get("report_path"),
                    "report_summary": report.get("summary"),
                    "path": artifact_path,
                    "status": artifact.get("status"),
                }
            )
            seen.add(artifact_path)
    return artifacts


def collect_canonical_contracts(
    project_root: Path,
    run_dir: Path,
    objective_id: str,
    current_phase: str,
) -> dict[str, dict[str, Any] | None]:
    if current_phase not in PHASE_SEQUENCE:
        return {"api_contract": None, "integration_contract": None}
    apps_root = project_root / "apps"
    known_app_roots = sorted(path for path in apps_root.iterdir() if path.is_dir()) if apps_root.exists() else []
    fallback_app_root = known_app_roots[0] if len(known_app_roots) == 1 else None
    current_app_root = find_objective_app_root(project_root, objective_id) or fallback_app_root
    current_index = PHASE_SEQUENCE.index(current_phase)
    selected: dict[str, dict[str, Any]] = {}
    for path in sorted((run_dir / "reports").glob("*.json")):
        payload = read_json(path)
        payload_phase = payload.get("phase")
        source_objective_id = payload.get("objective_id")
        if (
            not isinstance(source_objective_id, str)
            or payload_phase not in PHASE_SEQUENCE
            or PHASE_SEQUENCE.index(payload_phase) >= current_index
        ):
            continue
        report_app_root = find_objective_app_root(project_root, source_objective_id) or fallback_app_root
        if current_app_root is not None and report_app_root is not None and report_app_root != current_app_root:
            continue
        source_capability = infer_report_capability(str(payload.get("agent_role", "")))
        for artifact in payload.get("artifacts", []):
            artifact_path = str(artifact.get("path", "") or "").strip()
            if not artifact_path or artifact_path.startswith("inline://"):
                continue
            kind = contract_kind_for_reference(path=artifact_path)
            if kind not in {"api", "integration"}:
                continue
            authoritative_capability = authoritative_capability_for_contract_kind(kind)
            if source_capability != authoritative_capability:
                continue
            current = selected.get(kind)
            candidate_value = {
                "phase": payload_phase,
                "objective_id": source_objective_id,
                "capability": source_capability,
                "task_id": payload.get("task_id"),
                "report_path": str(path.relative_to(run_dir.parent.parent)),
                "path": artifact_path,
            }
            if current is None:
                selected[kind] = candidate_value
                continue
            if PHASE_SEQUENCE.index(payload_phase) >= PHASE_SEQUENCE.index(str(current["phase"])):
                selected[kind] = candidate_value
    return {
        "api_contract": selected.get("api"),
        "integration_contract": selected.get("integration"),
    }


def filter_noncanonical_contract_artifacts(
    artifacts: list[dict[str, Any]],
    *,
    canonical_contracts: dict[str, dict[str, Any] | None],
    allow_consumer_contracts: bool,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    canonical_paths = {
        kind: str(item.get("path", "") or "").strip()
        for kind, item in canonical_contracts.items()
        if isinstance(item, dict)
    }
    for artifact in artifacts:
        artifact_path = str(artifact.get("path", "") or "").strip()
        kind = contract_kind_for_reference(path=artifact_path)
        if kind is None:
            filtered.append(artifact)
            continue
        if kind == "consumer":
            if allow_consumer_contracts:
                filtered.append(artifact)
            continue
        canonical_key = f"{kind}_contract"
        if artifact_path and artifact_path == canonical_paths.get(canonical_key, ""):
            filtered.append(artifact)
    return filtered


def collect_related_app_prior_phase_reports(
    project_root: Path,
    run_dir: Path,
    objective_id: str,
    capability: str,
    current_phase: str,
) -> list[dict[str, Any]]:
    if current_phase not in PHASE_SEQUENCE:
        return []
    apps_root = project_root / "apps"
    known_app_roots = sorted(path for path in apps_root.iterdir() if path.is_dir()) if apps_root.exists() else []
    fallback_app_root = known_app_roots[0] if len(known_app_roots) == 1 else None
    current_app_root = find_objective_app_root(project_root, objective_id) or fallback_app_root
    current_index = PHASE_SEQUENCE.index(current_phase)
    reports: list[dict[str, Any]] = []
    for path in sorted((run_dir / "reports").glob("*.json")):
        payload = read_json(path)
        payload_phase = payload.get("phase")
        report_objective_id = payload.get("objective_id")
        if (
            not isinstance(report_objective_id, str)
            or report_objective_id == objective_id
            or payload_phase not in PHASE_SEQUENCE
            or PHASE_SEQUENCE.index(payload_phase) >= current_index
        ):
            continue
        report_app_root = find_objective_app_root(project_root, report_objective_id) or fallback_app_root
        if current_app_root is not None and report_app_root is not None and report_app_root != current_app_root:
            continue
        if current_app_root is not None and report_app_root is None and len(known_app_roots) > 1:
            continue
        agent_role = str(payload.get("agent_role", ""))
        if capability != "general" and f".{capability}-" not in agent_role:
            continue
        reports.append(
            {
                "phase": payload_phase,
                "capability": infer_report_capability(agent_role),
                "objective_id": report_objective_id,
                "task_id": payload["task_id"],
                "report_path": str(path.relative_to(run_dir.parent.parent)),
                "summary": compact_text(payload.get("summary", "")),
                "artifacts": compact_artifacts(payload.get("artifacts", [])),
                "open_issues_preview": compact_text_list(payload.get("open_issues", [])),
            }
        )
    if current_index > 0:
        immediately_previous_phase = PHASE_SEQUENCE[current_index - 1]
        immediate_reports = [report for report in reports if report["phase"] == immediately_previous_phase]
        if immediate_reports:
            reports = immediate_reports
    reports.sort(key=lambda item: (PHASE_SEQUENCE.index(item["phase"]), item["objective_id"], item["task_id"]))
    return reports


def is_frontend_consumption_contract_path(path: str) -> bool:
    return is_frontend_consumption_contract_reference(path)


def filter_backend_mvp_build_related_inputs(
    objective: dict[str, Any],
    *,
    capability: str,
    phase: str,
    related_reports: list[dict[str, Any]],
    related_artifacts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if phase != "mvp-build":
        return related_reports, related_artifacts
    objective_capabilities = [
        str(value).strip()
        for value in objective.get("capabilities", [])
        if isinstance(value, str) and str(value).strip()
    ]
    backend_context = capability == "backend" or (capability == "general" and objective_capabilities == ["backend"])
    if not backend_context:
        return related_reports, related_artifacts
    filtered_reports = [
        report for report in related_reports if str(report.get("capability", "") or "").strip() != "frontend"
    ]
    filtered_artifacts = [
        artifact
        for artifact in related_artifacts
        if not is_frontend_consumption_contract_path(str(artifact.get("path", "") or ""))
    ]
    return filtered_reports, filtered_artifacts


def select_related_prior_phase_reports_for_prompt(
    reports: list[dict[str, Any]],
    *,
    limit: int,
    minimum: int = 2,
) -> list[dict[str, Any]]:
    if not reports:
        return []
    selected_limit = min(len(reports), max(limit, minimum))
    ranked = sorted(reports, key=related_report_priority, reverse=True)
    return ranked[:selected_limit]


def related_report_priority(report: dict[str, Any]) -> tuple[int, int, int, str, str]:
    phase = str(report.get("phase", ""))
    summary = str(report.get("summary", ""))
    task_id = str(report.get("task_id", ""))
    objective_id = str(report.get("objective_id", ""))
    capability = str(report.get("capability", ""))
    haystack = f"{summary} {task_id} {objective_id}".lower()
    keyword_weights = (
        ("sqlite", 10),
        ("persistence", 8),
        ("contract", 7),
        ("schema", 6),
        ("api", 5),
        ("stack", 4),
        ("integration", 3),
        ("validation", 2),
        ("review bundle", -2),
    )
    keyword_score = sum(weight for keyword, weight in keyword_weights if keyword in haystack)
    capability_score = {
        "backend": 3,
        "frontend": 2,
        "middleware": 1,
    }.get(capability, 0)
    phase_score = PHASE_SEQUENCE.index(phase) if phase in PHASE_SEQUENCE else -1
    return (phase_score, keyword_score, capability_score, objective_id, task_id)


def infer_report_capability(agent_role: str) -> str | None:
    for capability in ("frontend", "backend", "middleware", "general", "shared-platform", "documentation", "qa"):
        if f".{capability}-" in agent_role:
            return capability
    return None


def compact_artifacts(artifacts: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for artifact in artifacts[:limit]:
        artifact_path = artifact.get("path")
        if not artifact_path:
            continue
        compacted.append(
            {
                "path": artifact_path,
                "status": artifact.get("status"),
            }
        )
    return compacted


def compact_text_list(values: list[str], *, limit: int = 3, max_length: int = 160) -> list[str]:
    compacted: list[str] = []
    for value in values[:limit]:
        compacted.append(compact_text(value, max_length=max_length))
    return compacted


def compact_validation_steps(validation: list[dict[str, Any]] | list[Any], *, limit: int = 3) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for item in validation[:limit]:
        if not isinstance(item, dict):
            continue
        compacted.append(
            {
                "id": item.get("id"),
                "command": compact_text(str(item.get("command", "")), max_length=120),
            }
        )
    return compacted


def compact_text(value: str, *, max_length: int = 240) -> str:
    normalized = " ".join(str(value).split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."


def summarize_existing_phase_tasks(tasks: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for task in tasks[:limit]:
        summaries.append(
            {
                "task_id": task["task_id"],
                "capability": task.get("capability"),
                "objective": compact_text(task.get("objective", ""), max_length=160),
                "execution_mode": task.get("execution_mode"),
                "parallel_policy": task.get("parallel_policy"),
                "owned_paths": list(task.get("owned_paths", []))[:4],
                "writes_existing_paths": list(task.get("writes_existing_paths", []))[:4],
                "shared_asset_ids": list(task.get("shared_asset_ids", []))[:6],
                "depends_on": list(task.get("depends_on", [])),
                "handoff_dependencies": list(task.get("handoff_dependencies", [])),
                "expected_outputs": compact_output_descriptors(list(task.get("expected_outputs", [])), limit=4),
            }
        )
    return summaries


def compact_goal_context(
    goal_markdown: str,
    *,
    objective_id: str | None = None,
    objective_title: str = "",
    objective_summary: str = "",
    objective_detail_limit: int = 6,
    section_max_length: int = 420,
    detail_max_length: int = 520,
) -> dict[str, Any]:
    parsed_goal = parse_goal_sections(goal_markdown)
    sections = parsed_goal["sections"]
    ordered_keys = [
        "Summary",
        "Objectives",
        "Users And Stakeholders",
        "Desired Outcomes",
        "Success Criteria",
        "Constraints",
        "In Scope",
        "Out Of Scope",
        "Existing Systems And Dependencies",
        "Known Risks",
        "Known Unknowns",
        "Discovery Expectations",
        "Design Expectations",
        "MVP Build Expectations",
        "Polish Expectations",
        "Human Approval Notes",
    ]
    compact_sections = {
        key: compact_text(sections[key], max_length=section_max_length)
        for key in ordered_keys
        if sections.get(key)
    }
    objective_details = {}
    detail_items = list(parsed_goal.get("objective_details", {}).items())
    if objective_id or objective_title or objective_summary:
        detail_items = prioritize_objective_detail_items(
            detail_items,
            objective_id=objective_id or "",
            objective_title=objective_title,
            objective_summary=objective_summary,
        )
    for key, value in detail_items[:objective_detail_limit]:
        objective_details[key] = compact_text(value, max_length=detail_max_length)
    return {
        "sections": compact_sections,
        "objective_details": objective_details,
    }


def prioritize_objective_detail_items(
    detail_items: list[tuple[str, str]],
    *,
    objective_id: str,
    objective_title: str,
    objective_summary: str,
) -> list[tuple[str, str]]:
    scored: list[tuple[int, int, tuple[str, str]]] = []
    for index, item in enumerate(detail_items):
        scored.append(
            (
                objective_detail_match_score(
                    item[0],
                    objective_id=objective_id,
                    objective_title=objective_title,
                    objective_summary=objective_summary,
                ),
                index,
                item,
            )
        )
    if not any(score for score, _, _ in scored):
        return detail_items
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [item for _, _, item in scored]


def objective_detail_match_score(
    detail_name: str,
    *,
    objective_id: str,
    objective_title: str,
    objective_summary: str,
) -> int:
    detail_slug = slug_like(detail_name)
    targets = [objective_id, objective_title, objective_summary]
    score = 0
    for target in targets:
        target_slug = slug_like(target)
        if not detail_slug or not target_slug:
            continue
        if detail_slug == target_slug:
            score = max(score, 100)
        elif detail_slug in target_slug:
            score = max(score, 80)
        elif target_slug in detail_slug:
            score = max(score, 70)
    detail_tokens = match_tokens(detail_name)
    objective_tokens = match_tokens(" ".join(targets))
    if detail_tokens and objective_tokens:
        score += 6 * len(detail_tokens & objective_tokens)
    return score


def match_tokens(text: str) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "basic",
        "for",
        "layer",
        "of",
        "simple",
        "the",
        "to",
        "workflow",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in stopwords
    }


def compact_objective_record(objective: dict[str, Any]) -> dict[str, Any]:
    compacted = {
        "objective_id": objective.get("objective_id"),
        "title": objective.get("title"),
        "summary": compact_text(objective.get("summary", ""), max_length=220),
    }
    if objective.get("success_criteria"):
        compacted["success_criteria"] = compact_text_list(list(objective.get("success_criteria", [])), limit=4, max_length=160)
    if objective.get("dependencies"):
        compacted["dependencies"] = list(objective.get("dependencies", []))[:6]
    return compacted


def compact_team_record(team: dict[str, Any]) -> dict[str, Any]:
    compacted_roles = []
    for role in team.get("roles", [])[:8]:
        compacted_roles.append(
            {
                "role_id": role.get("role_id"),
                "role_kind": role.get("role_kind"),
                "role_type": role.get("role_type"),
                "capability": role.get("capability"),
            }
        )
    return {
        "team_id": team.get("team_id"),
        "objective_id": team.get("objective_id"),
        "roles": compacted_roles,
    }


def compact_output_descriptors(values: list[Any], *, limit: int = 4, max_length: int = 140) -> list[Any]:
    compacted: list[Any] = []
    for descriptor in normalize_output_descriptors(values)[:limit]:
        kind = descriptor.get("kind")
        output_id = descriptor.get("output_id")
        if kind == "artifact":
            compacted.append(
                {
                    "kind": "artifact",
                    "output_id": output_id,
                    "path": descriptor.get("path"),
                }
            )
            continue
        if kind == "asset":
            compacted.append(
                {
                    "kind": "asset",
                    "output_id": output_id,
                    "asset_id": descriptor.get("asset_id"),
                    "path": descriptor.get("path"),
                }
            )
            continue
        compacted.append(
            {
                "kind": "assertion",
                "output_id": output_id,
                "description": compact_text(str(descriptor.get("description", "")), max_length=max_length),
                "evidence": {
                    "validation_ids": list(descriptor.get("evidence", {}).get("validation_ids", []))[:3]
                    if isinstance(descriptor.get("evidence"), dict)
                    else [],
                    "artifact_paths": list(descriptor.get("evidence", {}).get("artifact_paths", []))[:2]
                    if isinstance(descriptor.get("evidence"), dict)
                    else [],
                },
            }
        )
    return compacted


def compact_capability_lane(lane: dict[str, Any]) -> dict[str, Any]:
    return {
        "capability": lane.get("capability"),
        "objective": compact_text(lane.get("objective", ""), max_length=160),
        "inputs": list(lane.get("inputs", []))[:4],
        "expected_outputs": compact_output_descriptors(list(lane.get("expected_outputs", [])), limit=4),
        "depends_on": list(lane.get("depends_on", [])),
        "assigned_manager_role": lane.get("assigned_manager_role"),
    }


def compact_collaboration_edges(edges: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    compacted = []
    for edge in edges[:limit]:
        deliverable_refs = [
            {
                "output_id": item.get("output_id"),
                "input_ref": item.get("input_ref"),
            }
            for item in edge.get("deliverable_input_refs", [])[:3]
            if isinstance(item, dict)
        ]
        compacted.append(
            {
                "edge_id": edge.get("edge_id"),
                "from_capability": edge.get("from_capability"),
                "to_capability": edge.get("to_capability"),
                "to_role": edge.get("to_role"),
                "handoff_type": edge.get("handoff_type"),
                "deliverables": compact_output_descriptors(list(edge.get("deliverables", [])), limit=3),
                "deliverable_input_refs": deliverable_refs,
                "blocking": bool(edge.get("blocking")),
                "shared_asset_ids": list(edge.get("shared_asset_ids", []))[:6],
            }
        )
    return compacted


def annotate_handoff_deliverable_refs(edges: list[dict[str, Any]], *, field_name: str) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for edge_index, edge in enumerate(edges):
        enriched = dict(edge)
        refs: list[dict[str, Any]] = []
        for deliverable_index, descriptor in enumerate(normalize_output_descriptors(list(edge.get("deliverables", [])))):
            refs.append(
                {
                    "output_id": descriptor.get("output_id"),
                    "input_ref": f"Planning Inputs.{field_name}[{edge_index}].deliverables[{deliverable_index}]",
                }
            )
        enriched["deliverable_input_refs"] = refs
        annotated.append(enriched)
    return annotated


def compact_objective_outline_for_prompt(
    objective_outline: dict[str, Any],
    *,
    capability: str,
    edge_limit: int = 8,
    summary_max_length: int = 260,
    dependency_note_limit: int = 5,
    dependency_note_max_length: int = 160,
) -> dict[str, Any]:
    relevant_edges = [
        edge
        for edge in objective_outline.get("collaboration_edges", [])
        if edge.get("from_capability") == capability or edge.get("to_capability") == capability
    ][:edge_limit]
    related_capabilities = {capability}
    for edge in relevant_edges:
        if edge.get("from_capability"):
            related_capabilities.add(edge["from_capability"])
        if edge.get("to_capability"):
            related_capabilities.add(edge["to_capability"])
    return {
        "summary": compact_text(objective_outline.get("summary", ""), max_length=summary_max_length),
        "dependency_notes": compact_text_list(
            list(objective_outline.get("dependency_notes", [])),
            limit=dependency_note_limit,
            max_length=dependency_note_max_length,
        ),
        "capability_lanes": [
            compact_capability_lane(lane)
            for lane in objective_outline.get("capability_lanes", [])
            if lane.get("capability") in related_capabilities
        ],
        "relevant_collaboration_edges": compact_collaboration_edges(relevant_edges, limit=edge_limit),
    }


def build_planning_prompt_text(
    parts: list[str],
    runtime_context: dict[str, Any],
    planning_payload: dict[str, Any],
    *,
    repair_context: dict[str, Any] | None = None,
) -> str:
    contract_section = build_objective_contract_section(planning_payload)
    repair_section = build_manager_repair_section(repair_context)
    return "\n\n".join(
        parts
        + [
            contract_section,
            repair_section,
            "# Runtime Context\n\n```json\n" + json.dumps(runtime_context, indent=2, sort_keys=True) + "\n```",
            "# Planning Inputs\n\n```json\n" + json.dumps(planning_payload, indent=2, sort_keys=True) + "\n```",
        ]
    )


def build_capability_prompt_text(
    parts: list[str],
    runtime_context: dict[str, Any],
    planning_payload: dict[str, Any],
    *,
    repair_context: dict[str, Any] | None = None,
) -> str:
    contract_section = build_capability_contract_section(planning_payload)
    repair_section = build_manager_repair_section(repair_context)
    release_repair_section = build_release_repair_input_section(planning_payload)
    return "\n\n".join(
        parts
        + [
            contract_section,
            repair_section,
            release_repair_section,
            "# Runtime Context\n\n```json\n" + json.dumps(runtime_context, indent=2, sort_keys=True) + "\n```",
            "# Capability Planning Inputs\n\n```json\n" + json.dumps(planning_payload, indent=2, sort_keys=True) + "\n```",
        ]
    )


def build_capability_contract_section(planning_payload: dict[str, Any]) -> str:
    lines = [
        "# Exact Output Contract",
        "",
        "Use this section as the hard contract for the lane before you write the JSON plan.",
        "Your plan must cover exactly the final lane outputs listed in `Allowed Final Outputs`.",
        "Do not invent additional final lane outputs, report-only outputs, or handoff deliverables that are not listed here.",
        "If you need an intermediate artifact, keep it task-local and do not treat it as a final lane output or outbound handoff deliverable.",
        "",
        "If a file does not already exist and your task will create it, declare it in `expected_outputs`, not `writes_existing_paths`.",
        "If a required outbound handoff needs an output, the handoff source task must declare that same `output_id` in its own `expected_outputs`.",
        "If a same-phase dependency comes from another task in this capability lane, reference it as `Output of <task-id>`, not as a future `runs/<run>/artifacts/...`, `runs/<run>/reports/...`, or other landed file path.",
        "If a same-phase dependency comes from an inbound handoff deliverable, reference it with the exact `Planning Inputs.required_inbound_handoffs[...]` path, not the future file path.",
        "Do not place nonexistent future repo paths from same-phase work into task inputs.",
        "",
        "## Allowed Final Outputs",
    ]
    allowed_outputs = normalize_output_descriptors(list(planning_payload.get("allowed_final_outputs_exact", [])))
    if not allowed_outputs:
        lines.append("- None")
    else:
        for descriptor in allowed_outputs:
            lines.append(format_output_contract_line(descriptor))
    lines.extend(["", "## Existing Required Paths"])
    existing_required_paths = planning_payload.get("existing_required_output_paths_exact", [])
    if not existing_required_paths:
        lines.append("- None")
    else:
        lines.append(
            "- If a task emits one of these already-existing required files, it must include the same path in "
            "`writes_existing_paths` as well as `expected_outputs`."
        )
        for item in existing_required_paths:
            output_id = str(item.get("output_id") or "").strip()
            path = str(item.get("path") or "").strip()
            if output_id and path:
                lines.append(f"- `{output_id}` -> existing file `{path}`")
    lines.extend(["", "## Required Outbound Handoffs"])
    outbound_handoffs = planning_payload.get("required_outbound_handoffs_exact", [])
    if not outbound_handoffs:
        lines.append("- None")
    else:
        for handoff in outbound_handoffs:
            handoff_id = str(handoff.get("edge_id") or handoff.get("handoff_id") or "").strip()
            to_capability = str(handoff.get("to_capability") or "").strip()
            to_role = str(handoff.get("to_role") or "").strip()
            handoff_type = str(handoff.get("handoff_type") or "").strip()
            deliverables = normalize_output_descriptors(list(handoff.get("deliverables", [])))
            output_ids = [str(item.get("output_id") or "").strip() for item in deliverables if str(item.get("output_id") or "").strip()]
            lines.append(
                f"- `{handoff_id}` -> `{to_capability}` via `{to_role}`"
                + (f" ({handoff_type})" if handoff_type else "")
            )
            if output_ids:
                lines.append(f"  deliverable_output_ids: {', '.join(f'`{output_id}`' for output_id in output_ids)}")
            else:
                lines.append("  deliverable_output_ids: none")
    return "\n".join(lines)


def format_output_contract_line(descriptor: dict[str, Any]) -> str:
    kind = str(descriptor.get("kind") or "").strip() or "output"
    output_id = str(descriptor.get("output_id") or "").strip() or "unknown-output"
    path = str(descriptor.get("path") or "").strip()
    asset_id = str(descriptor.get("asset_id") or "").strip()
    description = str(descriptor.get("description") or "").strip()
    if path:
        detail = f"path `{path}`"
    elif asset_id:
        detail = f"asset_id `{asset_id}`"
    elif description:
        detail = description
    else:
        detail = descriptor_summary(descriptor)
    return f"- `{output_id}` ({kind}) -> {detail}"


def existing_required_output_paths_for_prompt(
    project_root: Path,
    run_id: str,
    descriptors: list[dict[str, Any]] | list[Any],
) -> list[dict[str, str]]:
    integration_root = integration_workspace_path(project_root, run_id)
    roots = [project_root]
    if integration_root.exists():
        roots.append(integration_root)
    existing: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for descriptor in normalize_output_descriptors(list(descriptors)):
        if str(descriptor.get("kind") or "").strip() not in {"artifact", "asset"}:
            continue
        output_id = str(descriptor.get("output_id") or "").strip()
        path = str(descriptor.get("path") or "").strip()
        if not output_id or not path:
            continue
        for root in roots:
            candidate = root / path
            if not candidate.exists() or candidate.is_dir():
                continue
            key = (output_id, path)
            if key not in seen:
                seen.add(key)
                existing.append({"output_id": output_id, "path": path})
            break
    return existing


def build_objective_contract_hints(
    project_root: Path,
    run_id: str,
    objective_id: str,
    capabilities: list[str],
    *,
    phase: str | None = None,
) -> dict[str, Any]:
    app_root = find_objective_app_root(project_root, objective_id)
    objective_root = find_objective_root(project_root, objective_id)
    objective_root_rel = relative_path_or_none(project_root, objective_root)
    run_roots = [
        f"runs/{run_id}/artifacts",
        f"runs/{run_id}/reports",
        f"runs/{run_id}/review-bundles",
    ]
    capability_output_roots: dict[str, list[str]] = {}
    for capability in capabilities:
        roots = list(
            capability_owned_path_hints(
                project_root,
                objective_id,
                capability,
                phase=phase,
            )
        )
        roots.extend(capability_owned_shared_workspace_paths(project_root, app_root, capability))
        if objective_root_rel:
            roots.append(objective_root_rel)
        roots.extend(run_roots)
        capability_output_roots[capability] = dedupe_prompt_strings(roots)
    return {
        "allowed_capabilities": capabilities,
        "capability_output_roots": capability_output_roots,
    }


def build_objective_contract_section(planning_payload: dict[str, Any]) -> str:
    contract_hints = dict(planning_payload.get("objective_contract_hints", {}))
    allowed_capabilities = [
        str(value).strip()
        for value in contract_hints.get("allowed_capabilities", [])
        if isinstance(value, str) and str(value).strip()
    ]
    lines = [
        "# Exact Objective Contract",
        "",
        "Use this section as the hard boundary for the outline before you return JSON.",
        "Create capability lanes only for the capabilities listed here.",
        "Keep each lane's artifact and asset outputs inside the allowed output surfaces for that capability.",
        "Do not invent lane output paths outside those surfaces.",
        "",
        "## Allowed Capabilities",
    ]
    if not allowed_capabilities:
        lines.append("- None")
    else:
        lines.append("- " + ", ".join(f"`{capability}`" for capability in allowed_capabilities))
    lines.extend(["", "## Allowed Output Surfaces By Capability"])
    capability_output_roots = contract_hints.get("capability_output_roots", {})
    if not capability_output_roots:
        lines.append("- None")
    else:
        for capability in allowed_capabilities:
            lines.append(f"- `{capability}`")
            for root in capability_output_roots.get(capability, []):
                lines.append(f"  - `{root}`")
    return "\n".join(lines)


def build_manager_repair_section(repair_context: dict[str, Any] | None) -> str:
    if not repair_context:
        return ""
    source = compact_text(str(repair_context.get("source", "repair")), max_length=80)
    reason = compact_text(str(repair_context.get("reason", "Repair the plan using the issues below.")), max_length=220)
    lines = [
        "# Manager Repair Context",
        "",
        "The previous plan or execution for this objective needs one bounded repair pass.",
        "Revise the plan only as much as needed to fix the exact issues below.",
        "Do not expand scope, invent new final outputs, or change ownership boundaries unless the repair context explicitly requires it.",
        "",
        f"- Source: `{source}`",
        f"- Reason: {reason}",
    ]
    bundle_id = str(repair_context.get("bundle_id", "")).strip()
    if bundle_id:
        lines.append(f"- Rejected bundle: `{bundle_id}`")
    release_validation_command = str(repair_context.get("release_validation_command", "")).strip()
    if release_validation_command:
        lines.append(f"- Release validation command: `{release_validation_command}`")
    release_validation_report_path = str(repair_context.get("release_validation_report_path", "")).strip()
    if release_validation_report_path:
        lines.append(f"- Release validation report: `{release_validation_report_path}`")
    focus_paths = repair_focus_paths(repair_context)
    if focus_paths:
        lines.append("- Focus paths: " + ", ".join(f"`{path}`" for path in focus_paths[:6]))
    task_ids = [
        str(value).strip()
        for value in repair_context.get("included_task_ids", [])
        if isinstance(value, str) and str(value).strip()
    ]
    if task_ids:
        lines.append("- Included tasks: " + ", ".join(f"`{task_id}`" for task_id in task_ids))
    rejection_reasons = [
        compact_text(str(value), max_length=220)
        for value in repair_context.get("rejection_reasons", [])
        if isinstance(value, str) and str(value).strip()
    ]
    lines.extend(["", "## Exact Issues To Fix"])
    if not rejection_reasons:
        lines.append("- None")
    else:
        for reason_line in rejection_reasons:
            lines.append(f"- {reason_line}")
    return "\n".join(lines)


def build_release_repair_input_section(planning_payload: dict[str, Any]) -> str:
    input_refs = [
        str(value).strip()
        for value in planning_payload.get("release_repair_input_refs", [])
        if isinstance(value, str) and str(value).strip()
    ]
    if not input_refs:
        return ""
    inputs = dict(planning_payload.get("release_repair_inputs") or {})
    lines = [
        "# Canonical Release Repair Inputs",
        "",
        "For inherited current-run polish evidence, use only these exact `Planning Inputs.release_repair_inputs.*` references.",
        "Do not write literal `runs/<run>/reports/...`, `runs/<run>/bundles/...`, or `runs/<run>/phase-reports/...` paths into task inputs.",
        "",
    ]
    for ref in input_refs:
        alias = ref.removeprefix("Planning Inputs.release_repair_inputs.").removesuffix(".path")
        entry = inputs.get(alias, {})
        path_value = str(entry.get("path") or "").strip()
        label = str(entry.get("label") or alias).strip()
        if path_value:
            lines.append(f"- `{ref}` -> `{path_value}` ({label})")
        else:
            lines.append(f"- `{ref}`")
    return "\n".join(lines)


def build_task_contract_section(task: dict[str, Any]) -> str:
    expected_outputs = normalize_output_descriptors(list(task.get("expected_outputs", [])))
    writes_existing_paths = [
        str(value).strip()
        for value in task.get("writes_existing_paths", [])
        if isinstance(value, str) and str(value).strip()
    ]
    input_refs = [
        str(value).strip()
        for value in task.get("inputs", [])
        if isinstance(value, str) and str(value).strip()
    ]
    lines = [
        "# Exact Task Contract",
        "",
        "Treat this section as the execution boundary for the task.",
        "Complete the task only by producing the required outputs below and staying inside the declared write scope.",
        "Do not invent additional final outputs or change files outside the listed scope.",
        "",
        "## Required Outputs",
    ]
    if not expected_outputs:
        lines.append("- None")
    else:
        for descriptor in expected_outputs:
            lines.append(format_output_contract_line(descriptor))
    lines.extend(["", "## Allowed Existing-File Edits"])
    if not writes_existing_paths:
        lines.append("- None")
    else:
        for path in writes_existing_paths:
            lines.append(f"- `{path}`")
    lines.extend(["", "## Declared Inputs"])
    if not input_refs:
        lines.append("- None")
    else:
        for input_ref in input_refs:
            lines.append(f"- `{input_ref}`")
    return "\n".join(lines)


def dedupe_prompt_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def is_compact_release_repair_context(repair_context: dict[str, Any] | None) -> bool:
    if not isinstance(repair_context, dict):
        return False
    return str(repair_context.get("source") or "").strip() == "polish_release_validation" and bool(
        repair_context.get("compact_prompt", True)
    )


def repair_prompt_compaction_profile(repair_context: dict[str, Any] | None) -> dict[str, Any] | None:
    if not is_compact_release_repair_context(repair_context):
        return None
    if bool(repair_context.get("compact_retry_used")):
        return {
            "level": "aggressive",
            "reason": "Second-pass compact release-repair prompt after a stalled planning turn.",
            "limits": {
                "existing_tasks": 0,
                "prior_reports": 0,
                "prior_artifacts": 0,
                "catalog_reports": 0,
                "catalog_artifacts": 0,
                "outline_edges": 1,
                "objective_details": 0,
                "section_max_length": 80,
                "detail_max_length": 100,
                "outline_summary_max_length": 90,
                "dependency_note_limit": 0,
                "dependency_note_max_length": 80,
            },
        }
    return {
        "level": "aggressive",
        "reason": "Compact release-repair planning prompt focused on the failing owned surface only.",
        "limits": {
            "existing_tasks": 0,
            "prior_reports": 1,
            "prior_artifacts": 1,
            "catalog_reports": 1,
            "catalog_artifacts": 1,
            "outline_edges": 1,
            "objective_details": 1,
            "section_max_length": 120,
            "detail_max_length": 140,
            "outline_summary_max_length": 120,
            "dependency_note_limit": 1,
            "dependency_note_max_length": 100,
        },
    }


def repair_focus_paths(repair_context: dict[str, Any] | None) -> list[str]:
    if not isinstance(repair_context, dict):
        return []
    values = repair_context.get("focus_paths", [])
    if not isinstance(values, list):
        return []
    return dedupe_prompt_strings([str(value).strip() for value in values if isinstance(value, str) and str(value).strip()])


def payload_record_matches_focus_paths(record: Any, focus_paths: list[str]) -> bool:
    if not focus_paths:
        return False
    serialized = json.dumps(record, sort_keys=True)
    return any(path in serialized for path in focus_paths)


def select_records_for_release_repair(records: list[Any], focus_paths: list[str], *, limit: int) -> list[Any]:
    if limit <= 0:
        return []
    matching = [record for record in records if payload_record_matches_focus_paths(record, focus_paths)]
    if matching:
        return matching[:limit]
    return records[:limit]


def compact_team_record_for_capability(team: dict[str, Any], capability: str | None) -> dict[str, Any]:
    compacted = compact_team_record(team)
    if not capability:
        return compacted
    filtered_roles = [
        role
        for role in compacted.get("roles", [])
        if role.get("capability") in {capability, None, ""}
        or role.get("role_kind") in {"acceptance", "objective_management"}
    ]
    compacted["roles"] = filtered_roles[:4]
    return compacted


def apply_release_repair_payload_compaction(
    compacted: dict[str, Any],
    *,
    repair_context: dict[str, Any],
    capability: str | None = None,
) -> dict[str, Any]:
    focus_paths = repair_focus_paths(repair_context)
    updated = dict(compacted)
    goal_context = dict(updated.get("goal_context") or {})
    goal_sections = dict(goal_context.get("sections") or {})
    updated["goal_context"] = {
        "sections": {
            key: goal_sections[key]
            for key in ("Success Criteria", "Polish Expectations", "Constraints")
            if key in goal_sections
        },
        "objective_details": {},
    }
    if capability:
        updated["team"] = compact_team_record_for_capability(dict(updated.get("team") or {}), capability)
        contract_hints = dict(updated.get("objective_contract_hints") or {})
        capability_output_roots = dict(contract_hints.get("capability_output_roots") or {})
        updated["objective_contract_hints"] = {
            "allowed_capabilities": [capability],
            "capability_output_roots": {
                capability: list(capability_output_roots.get(capability, []))[:4],
            },
        }
    updated["existing_phase_tasks"] = []
    updated["existing_phase_tasks_by_id"] = {}
    updated["existing_capability_tasks"] = []
    updated["existing_capability_tasks_by_id"] = {}
    updated["prior_phase_reports"] = select_records_for_release_repair(
        list(updated.get("prior_phase_reports") or []),
        focus_paths,
        limit=1,
    )
    updated["prior_phase_artifacts"] = select_records_for_release_repair(
        list(updated.get("prior_phase_artifacts") or []),
        focus_paths,
        limit=2,
    )
    updated["related_prior_phase_reports"] = select_records_for_release_repair(
        list(updated.get("related_prior_phase_reports") or []),
        focus_paths,
        limit=1,
    )
    updated["related_prior_phase_artifacts"] = select_records_for_release_repair(
        list(updated.get("related_prior_phase_artifacts") or []),
        focus_paths,
        limit=2,
    )
    updated["approved_inputs_catalog"] = {
        "report_paths": list(updated.get("approved_inputs_catalog", {}).get("report_paths", []))[:1],
        "artifact_paths": list(updated.get("approved_inputs_catalog", {}).get("artifact_paths", []))[:2],
        "phase_report_paths": [],
    }
    return updated


def release_repair_alias_name(path: str) -> str:
    normalized = str(path).strip().replace("\\", "/")
    stem = Path(normalized).stem.lower()
    alias = re.sub(r"[^a-z0-9]+", "_", stem).strip("_")
    if "/reports/" in normalized:
        return f"report_{alias or 'artifact'}"
    if "/bundles/" in normalized:
        return f"bundle_{alias or 'artifact'}"
    if "/phase-reports/" in normalized:
        return f"phase_report_{alias or 'artifact'}"
    return f"artifact_{alias or 'input'}"


def build_release_repair_inputs(
    project_root: Path,
    run_id: str,
    objective_id: str,
    *,
    capability: str | None,
    phase: str,
) -> dict[str, dict[str, Any]]:
    if phase != "polish" or phase not in PHASE_SEQUENCE:
        return {}
    current_index = PHASE_SEQUENCE.index(phase)
    if current_index == 0:
        return {}
    previous_phase = PHASE_SEQUENCE[current_index - 1]
    run_dir = project_root / "runs" / run_id
    entries: dict[str, dict[str, Any]] = {}
    seen_paths: set[str] = set()

    def add_entry(path_value: str, *, kind: str, label: str, summary: str | None = None) -> None:
        normalized = str(path_value).strip()
        if not normalized or normalized in seen_paths:
            return
        seen_paths.add(normalized)
        alias = release_repair_alias_name(normalized)
        counter = 2
        base_alias = alias
        while alias in entries:
            alias = f"{base_alias}_{counter}"
            counter += 1
        entry = {
            "kind": kind,
            "label": label,
            "path": normalized,
        }
        if summary:
            entry["summary"] = compact_text(summary, max_length=180)
        entries[alias] = entry

    for path in sorted((run_dir / "reports").glob("*.json")):
        payload = read_json(path)
        if payload.get("objective_id") != objective_id or payload.get("phase") != previous_phase:
            continue
        if capability and capability != "general":
            report_capability = infer_report_capability(str(payload.get("agent_role", "")))
            if report_capability not in {capability, None}:
                continue
        add_entry(
            str(path.relative_to(project_root)),
            kind="report_json",
            label=f"{previous_phase} report {path.stem}",
            summary=str(payload.get("summary") or ""),
        )

    for path in sorted((run_dir / "bundles").glob("*.json")):
        payload = read_json(path)
        if payload.get("objective_id") != objective_id or payload.get("phase") != previous_phase:
            continue
        add_entry(
            str(path.relative_to(project_root)),
            kind="bundle_json",
            label=f"{previous_phase} bundle {payload.get('bundle_id') or path.stem}",
            summary=f"status={payload.get('status', 'unknown')}",
        )

    phase_report_path = run_dir / "phase-reports" / f"{previous_phase}.json"
    if phase_report_path.exists():
        add_entry(
            str(phase_report_path.relative_to(project_root)),
            kind="phase_report_json",
            label=f"{previous_phase} phase report",
        )
    return entries


def release_repair_input_refs(release_repair_inputs: dict[str, dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for alias in release_repair_inputs:
        refs.append(f"Planning Inputs.release_repair_inputs.{alias}.path")
    return refs


def maybe_escalate_planning_compaction(
    project_root: Path,
    run_id: str,
    phase: str,
    prompt_text: str,
    *,
    current: dict[str, Any],
) -> dict[str, Any]:
    stats = prompt_metrics(prompt_text)
    if current["level"] == "aggressive":
        return current
    if stats["prompt_char_count"] >= 18000:
        upgraded = planning_compaction_profile(project_root, run_id, phase)
        if upgraded["level"] == "standard":
            upgraded = {
                "level": "aggressive",
                "reason": "Current prompt exceeded the hard planning size budget; escalating to aggressive compaction.",
                "limits": {
                    "existing_tasks": 2,
                    "prior_reports": 1,
                    "prior_artifacts": 2,
                    "catalog_reports": 3,
                    "catalog_artifacts": 3,
                    "outline_edges": 2,
                    "objective_details": 1,
                    "section_max_length": 260,
                    "detail_max_length": 320,
                    "outline_summary_max_length": 180,
                    "dependency_note_limit": 3,
                    "dependency_note_max_length": 120,
                },
            }
        elif upgraded["level"] == "compact":
            upgraded = {
                "level": "aggressive",
                "reason": "Current prompt exceeded the hard planning size budget; escalating from compact to aggressive compaction.",
                "limits": {
                    "existing_tasks": 2,
                    "prior_reports": 1,
                    "prior_artifacts": 2,
                    "catalog_reports": 3,
                    "catalog_artifacts": 3,
                    "outline_edges": 2,
                    "objective_details": 1,
                    "section_max_length": 260,
                    "detail_max_length": 320,
                    "outline_summary_max_length": 180,
                    "dependency_note_limit": 3,
                    "dependency_note_max_length": 120,
                },
            }
        return upgraded
    if current["level"] == "standard" and stats["prompt_char_count"] >= 14000:
        return {
            "level": "compact",
            "reason": "Current prompt exceeded the compact planning size budget; escalating to compact mode.",
            "limits": {
                "existing_tasks": 6,
                "prior_reports": 4,
                "prior_artifacts": 5,
                "catalog_reports": 8,
                "catalog_artifacts": 8,
                "outline_edges": 6,
                "objective_details": 4,
                "section_max_length": 420,
                "detail_max_length": 520,
                "outline_summary_max_length": 260,
                "dependency_note_limit": 5,
                "dependency_note_max_length": 160,
            },
        }
    return current


def slug_like(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def resolve_goal_context_dotted_ref(planning_payload: dict[str, Any], dotted_path: str) -> Any | None:
    goal_markdown = planning_payload.get("goal_markdown")
    if not isinstance(goal_markdown, str) or not dotted_path.startswith("goal_context."):
        return None
    parts = dotted_path.split(".")
    if len(parts) < 3:
        return None
    parsed_goal = parse_goal_sections(goal_markdown)
    category = parts[1]
    ref_name = ".".join(parts[2:]).strip()
    if not ref_name:
        return None
    if category == "sections":
        resolved = get_case_insensitive(parsed_goal["sections"], ref_name)
    elif category == "objective_details":
        resolved = get_case_insensitive(parsed_goal["objective_details"], ref_name)
    else:
        return None
    if isinstance(resolved, dict) and resolved.get("missing_section") == ref_name:
        return None
    return resolved


def collect_completed_phase_reports(run_dir: Path, phase_plan: dict[str, Any], current_phase: str) -> list[str]:
    current_index = PHASE_SEQUENCE.index(current_phase)
    completed_paths: list[str] = []
    for entry in phase_plan["phases"]:
        phase = entry["phase"]
        if phase not in PHASE_SEQUENCE or PHASE_SEQUENCE.index(phase) >= current_index or entry["status"] != "complete":
            continue
        for suffix in (".json", ".md"):
            path = run_dir / "phase-reports" / f"{phase}{suffix}"
            if path.exists():
                completed_paths.append(str(path.relative_to(run_dir.parent.parent)))
    return completed_paths


def resolve_prior_phase_context_ref(project_root: Path, input_ref: str, planning_payload: dict[str, Any]) -> Any | None:
    catalog = planning_payload.get("approved_inputs_catalog", {})
    report_paths = catalog.get("report_paths", [])
    artifact_paths = catalog.get("artifact_paths", [])
    phase_report_paths = catalog.get("phase_report_paths", [])
    if not report_paths and not artifact_paths and not phase_report_paths:
        return None

    keywords = extract_keywords(input_ref)
    if not keywords:
        return None

    matched_reports = []
    for report in planning_payload.get("prior_phase_reports", []):
        searchable = " ".join(
            [
                report["task_id"],
                report["phase"],
                report["report_path"],
                report.get("summary", ""),
                " ".join(artifact.get("path", "") for artifact in report.get("artifacts", [])),
            ]
        ).lower()
        score = keyword_match_score(keywords, searchable)
        if score > 0:
            matched_reports.append((score, report))

    matched_artifacts = []
    for artifact in planning_payload.get("prior_phase_artifacts", []):
        searchable = " ".join([artifact["path"], artifact["source_task_id"], artifact["phase"]]).lower()
        score = keyword_match_score(keywords, searchable)
        if score > 0:
            matched_artifacts.append((score, artifact))

    matched_phase_reports = []
    for rel_path in phase_report_paths:
        score = keyword_match_score(keywords, rel_path.lower())
        if score > 0:
            matched_phase_reports.append((score, rel_path))

    if not matched_reports and not matched_artifacts and not matched_phase_reports:
        return None

    response: dict[str, Any] = {}
    if matched_reports:
        response["matched_prior_reports"] = [
            {
                "report_path": item["report_path"],
                "report": read_json(project_root / item["report_path"]),
            }
            for _, item in sorted(matched_reports, key=lambda pair: (-pair[0], pair[1]["report_path"]))[:3]
        ]
    if matched_artifacts:
        response["matched_prior_artifacts"] = [
            {
                "path": item["path"],
                "content": (
                    read_path(project_root / item["path"])
                    if (project_root / item["path"]).exists()
                    else fallback_report_artifact(planning_payload, item["path"])
                ),
            }
            for _, item in sorted(matched_artifacts, key=lambda pair: (-pair[0], pair[1]["path"]))[:3]
        ]
    if matched_phase_reports:
        response["matched_phase_reports"] = [
            {
                "path": rel_path,
                "content": read_path(project_root / rel_path),
            }
            for _, rel_path in sorted(matched_phase_reports, key=lambda pair: (-pair[0], pair[1]))[:2]
        ]
    return response


def extract_keywords(input_ref: str) -> list[str]:
    stopwords = {
        "the",
        "and",
        "for",
        "from",
        "with",
        "that",
        "this",
        "into",
        "only",
        "approved",
        "package",
        "objective",
        "success",
        "criteria",
        "inputs",
        "input",
        "rules",
        "using",
        "through",
        "build",
        "phase",
    }
    tokens = []
    for raw_token in input_ref.replace("/", " ").replace("-", " ").replace("_", " ").split():
        normalized = "".join(ch for ch in raw_token.lower() if ch.isalnum())
        if len(normalized) < 3 or normalized in stopwords:
            continue
        tokens.append(normalized)
    return tokens


def keyword_match_score(keywords: list[str], searchable: str) -> int:
    return sum(1 for keyword in keywords if keyword in searchable)


def parse_goal_sections(goal_markdown: str) -> dict[str, Any]:
    sections: dict[str, str] = {}
    objective_detail_sections: dict[str, str] = {}
    current_h2: str | None = None
    current_h3: str | None = None
    h2_lines: list[str] = []
    h3_lines: list[str] = []

    def flush_h3() -> None:
        nonlocal current_h3, h3_lines
        if current_h2 == "Objective Details" and current_h3 is not None:
            objective_detail_sections[current_h3] = "\n".join(h3_lines).strip()
        h3_lines = []

    def flush_h2() -> None:
        nonlocal current_h2, h2_lines
        if current_h2 is not None:
            sections[current_h2] = "\n".join(h2_lines).strip()
        h2_lines = []

    for raw_line in goal_markdown.splitlines():
        if raw_line.startswith("## "):
            flush_h3()
            flush_h2()
            current_h2 = raw_line[3:].strip()
            current_h3 = None
            continue
        if raw_line.startswith("### "):
            flush_h3()
            current_h3 = raw_line[4:].strip()
            continue
        if current_h3 is not None:
            h3_lines.append(raw_line)
        elif current_h2 is not None:
            h2_lines.append(raw_line)

    flush_h3()
    flush_h2()
    return {"sections": sections, "objective_details": objective_detail_sections}


def split_section_reference(section_text: str) -> list[str]:
    normalized = section_text.replace(" and ", ", ").replace(" / ", ", ")
    parts = [part.strip() for part in normalized.split(",") if part.strip()]
    return [normalize_section_ref(part) for part in parts]


def normalize_section_ref(section_ref: str) -> str:
    normalized = section_ref.strip().rstrip(".")
    lower = normalized.lower()
    if lower.startswith("goal markdown "):
        normalized = normalized[len("Goal markdown ") :]
        lower = normalized.lower()
    if lower.startswith("planning inputs "):
        normalized = normalized[len("Planning Inputs ") :]
        lower = normalized.lower()
    if lower.startswith("planning input "):
        normalized = normalized[len("Planning input ") :]
        lower = normalized.lower()
    if lower.endswith(" sections"):
        normalized = normalized[: -len(" sections")]
        lower = normalized.lower()
    if lower.endswith(" section"):
        normalized = normalized[: -len(" section")]
        lower = normalized.lower()
    normalized = normalized.replace("in-scope", "In Scope").replace("out-of-scope", "Out Of Scope")
    normalized = normalized.replace("MVP Build Expectations", "MVP Build Expectations")
    return normalized.strip()


def match_goal_lines(goal_markdown: str, input_ref: str) -> list[str]:
    keywords = [token.lower() for token in input_ref.replace(":", " ").split() if len(token) > 3]
    lines = []
    for line in goal_markdown.splitlines():
        lower = line.lower()
        if any(keyword in lower for keyword in keywords):
            lines.append(line)
    return lines


def resolve_goal_sections(parsed_goal: dict[str, Any], section_refs: list[str]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for ref in section_refs:
        if ref.startswith("Objective Details ->"):
            detail_name = ref.split("->", 1)[1].strip()
            resolved[ref] = get_case_insensitive(parsed_goal["objective_details"], detail_name)
            continue
        resolved[ref] = get_case_insensitive(parsed_goal["sections"], ref)
    return resolved


def get_case_insensitive(mapping: dict[str, Any], key: str) -> Any:
    for candidate_key, value in mapping.items():
        if candidate_key.lower() == key.lower():
            return value
    return {"missing_section": key}


def _infer_role_kind(role_name: str) -> str:
    if role_name == "acceptance-manager":
        return "acceptance-manager"
    if role_name.endswith("manager"):
        return "manager"
    return "worker"
