from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .filesystem import read_json, read_text, write_json, write_text
from .objective_roots import find_objective_root
from .planner import assert_active_phase

PHASE_SEQUENCE = ["discovery", "design", "mvp-build", "polish"]


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
    objective_root = find_objective_root(project_root, objective_id)
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
    runtime_context = build_task_runtime_context(project_root, run_id, task, files_loaded, metadata["prompt_path"], role_kind)
    resolved_inputs = resolve_task_inputs(project_root, run_id, task, runtime_context)
    metadata["resolved_input_refs"] = sorted(resolved_inputs)
    prompt_text = "\n\n".join(
        parts
        + [
            "# Runtime Context\n\n```json\n" + json.dumps(runtime_context, indent=2, sort_keys=True) + "\n```",
            "# Resolved Inputs\n\n```json\n" + json.dumps(resolved_inputs, indent=2, sort_keys=True) + "\n```",
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
    files_loaded: list[str] = []
    parts: list[str] = []

    def add(path: Path) -> None:
        parts.append(read_text(path))
        files_loaded.append(str(path.relative_to(project_root)))

    add(project_root / "orchestrator" / "roles" / "base" / "company.md")
    add(project_root / "orchestrator" / "roles" / "base" / "manager.md")
    add(project_root / "orchestrator" / "roles" / "base" / "objective-manager.md")

    objective_root = find_objective_root(project_root, objective_id)
    add(objective_root / "charter.md")
    objective_manager_path = objective_root / "approved" / "objective-manager.md"
    if objective_manager_path.exists():
        add(objective_manager_path)

    add(project_root / "orchestrator" / "phase-overlays" / f"{phase}.md")

    planning_payload = build_planning_payload(project_root, run_id, objective_id)
    runtime_context = build_planning_runtime_context(
        objective_id=objective_id,
        phase=phase,
        team=planning_payload["team"],
        files_loaded=files_loaded,
    )
    prompt_text = "\n\n".join(
        parts
        + [
            "# Runtime Context\n\n```json\n" + json.dumps(runtime_context, indent=2, sort_keys=True) + "\n```",
            "# Planning Inputs\n\n```json\n" + json.dumps(planning_payload, indent=2, sort_keys=True) + "\n```",
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


def build_task_runtime_context(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    files_loaded: list[str],
    prompt_path: str,
    role_kind: str,
) -> dict[str, Any]:
    objective_payload = build_planning_payload(project_root, run_id, task["objective_id"])
    return {
        "available_roles": [
            f"objectives.{task['objective_id']}.{role['role_id']}" for role in objective_payload["team"]["roles"]
        ],
        "objective_id": task["objective_id"],
        "phase": task["phase"],
        "planning_schema": "objective-plan.v1",
        "prompt_layers_loaded": files_loaded,
        "prompt_log_path": prompt_path,
        "role_kind": role_kind,
        "worker_roles": [
            f"objectives.{task['objective_id']}.{role['role_id']}"
            for role in objective_payload["team"]["roles"]
            if role["role_kind"] == "worker"
        ],
    }


def build_planning_runtime_context(
    *, objective_id: str, phase: str, team: dict[str, Any], files_loaded: list[str]
) -> dict[str, Any]:
    return {
        "prompt_layers_loaded": files_loaded,
        "planning_schema": "objective-plan.v1",
        "objective_id": objective_id,
        "phase": phase,
        "available_roles": [f"objectives.{objective_id}.{role['role_id']}" for role in team["roles"]],
        "worker_roles": [f"objectives.{objective_id}.{role['role_id']}" for role in team["roles"] if role["role_kind"] == "worker"],
    }


def build_planning_payload(project_root: Path, run_id: str, objective_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    phase_plan = read_json(run_dir / "phase-plan.json")
    phase = phase_plan["current_phase"]
    objective_map = read_json(run_dir / "objective-map.json")
    team_registry = read_json(run_dir / "team-registry.json")
    objective = next(item for item in objective_map["objectives"] if item["objective_id"] == objective_id)
    team = next(item for item in team_registry["teams"] if item["objective_id"] == objective_id)
    existing_phase_tasks = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        task = read_json(path)
        if task["phase"] == phase and task["objective_id"] == objective_id:
            existing_phase_tasks.append(task)
    all_prior_phase_reports = collect_prior_phase_reports(run_dir, objective_id, phase)
    prior_phase_reports = select_detailed_prior_phase_reports(all_prior_phase_reports, phase)
    prior_phase_artifacts = collect_prior_phase_artifacts(project_root, all_prior_phase_reports)
    prior_phase_phase_reports = collect_completed_phase_reports(run_dir, phase_plan, phase)
    return {
        "goal_markdown": read_text(run_dir / "goal.md"),
        "objective": objective,
        "team": team,
        "existing_phase_tasks": existing_phase_tasks,
        "prior_phase_reports": prior_phase_reports,
        "prior_phase_artifacts": prior_phase_artifacts,
        "approved_inputs_catalog": {
            "report_paths": [item["report_path"] for item in all_prior_phase_reports],
            "artifact_paths": [item["path"] for item in prior_phase_artifacts],
            "phase_report_paths": prior_phase_phase_reports,
        },
    }


def resolve_task_inputs(
    project_root: Path, run_id: str, task: dict[str, Any], runtime_context: dict[str, Any]
) -> dict[str, Any]:
    planning_payload = build_planning_payload(project_root, run_id, task["objective_id"])
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
    return resolved


def resolve_input_reference(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    input_ref: str,
    *,
    runtime_context: dict[str, Any],
    planning_payload: dict[str, Any],
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
        return lookup_dotted_path(planning_payload, normalized_ref.removeprefix("Planning Inputs."))
    if normalized_lower.startswith("output of "):
        task_id = normalized_ref.split(" ", 2)[2].strip()
        return resolve_task_output(project_root, run_id, task_id)
    if normalized_lower.startswith("outputs from "):
        task_id = normalized_ref.split(" ", 2)[2].strip()
        return resolve_task_output(project_root, run_id, task_id)
    if normalized_ref.startswith("runs/"):
        candidate = project_root / normalized_ref
        if candidate.exists():
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
    candidate = project_root / normalized_ref
    if candidate.exists():
        return read_path(candidate)
    return {"unresolved_input_ref": normalized_ref}


def preview_resolved_inputs(project_root: Path, run_id: str, task: dict[str, Any]) -> dict[str, Any]:
    role_kind = _infer_role_kind(task["assigned_role"].split(".")[-1])
    runtime_context = build_task_runtime_context(project_root, run_id, task, [], "", role_kind)
    return resolve_task_inputs(project_root, run_id, task, runtime_context)


def resolve_task_output(project_root: Path, run_id: str, task_id: str) -> Any:
    run_dir = project_root / "runs" / run_id
    report_path = run_dir / "reports" / f"{task_id}.json"
    if report_path.exists():
        return read_json(report_path)
    execution_path = run_dir / "executions" / f"{task_id}.json"
    if execution_path.exists():
        return read_json(execution_path)
    return {"missing_task_output": task_id}


def read_path(path: Path) -> Any:
    if path.suffix == ".json":
        return read_json(path)
    return read_text(path)


def lookup_dotted_path(payload: Any, dotted_path: str) -> Any:
    current = payload
    if not dotted_path:
        return current
    for part in dotted_path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        return {"missing_path": dotted_path}
    return current


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
                "task_id": payload["task_id"],
                "report_path": str(path.relative_to(run_dir.parent.parent)),
                "summary": compact_text(payload.get("summary", "")),
                "artifacts": compact_artifacts(payload.get("artifacts", [])),
                "open_issues_preview": compact_text_list(payload.get("open_issues", [])),
                "dependency_impact_preview": compact_text_list(payload.get("dependency_impact", [])),
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
            candidate = project_root / artifact_path
            if not candidate.exists():
                continue
            artifacts.append(
                {
                    "phase": report["phase"],
                    "source_task_id": report["task_id"],
                    "path": artifact_path,
                    "status": artifact.get("status"),
                }
            )
            seen.add(artifact_path)
    return artifacts


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


def compact_text(value: str, *, max_length: int = 240) -> str:
    normalized = " ".join(str(value).split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3].rstrip() + "..."


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
                "content": read_path(project_root / item["path"]),
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
