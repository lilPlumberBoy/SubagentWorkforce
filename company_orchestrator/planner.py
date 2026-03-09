from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .constants import PHASES
from .filesystem import ensure_dir, read_json, read_text, write_json, write_text
from .live import initialize_live_run
from .objective_roots import find_objective_root
from .schemas import validate_document

CAPABILITY_KEYWORDS = {
    "frontend": ("frontend", "ui", "web", "screen", "client"),
    "backend": ("backend", "api", "service", "server", "database"),
    "middleware": ("middleware", "integration", "queue", "worker", "sync"),
    "shared-platform": ("shared", "platform", "sdk", "auth", "core"),
    "documentation": ("docs", "documentation", "guide"),
    "qa": ("test", "qa", "quality"),
}


def initialize_run(project_root: Path, run_id: str, goal_text: str) -> Path:
    run_dir = project_root / "runs" / run_id
    for child in [
        "tasks",
        "reports",
        "bundles",
        "collaboration",
        "phase-reports",
        "changes",
        "prompt-logs",
        "executions",
        "manager-runs",
        "live",
    ]:
        ensure_dir(run_dir / child)
    write_text(run_dir / "goal.md", goal_text)
    write_json(run_dir / "phase-plan.json", default_phase_plan(run_id))
    write_json(run_dir / "objective-map.json", default_objective_map(run_id))
    write_json(run_dir / "team-registry.json", default_team_registry(run_id))
    initialize_live_run(project_root, run_id)
    return run_dir


def assert_active_phase(project_root: Path, run_id: str, requested_phase: str) -> None:
    phase_plan = read_json(project_root / "runs" / run_id / "phase-plan.json")
    if phase_plan["current_phase"] != requested_phase:
        raise ValueError(
            f"Task phase {requested_phase} does not match active phase {phase_plan['current_phase']} for run {run_id}"
        )


def default_phase_plan(run_id: str) -> dict[str, Any]:
    return {
        "schema": "phase-plan.v1",
        "run_id": run_id,
        "current_phase": "discovery",
        "phases": [
            {"phase": phase, "status": "active" if phase == "discovery" else "locked", "human_approved": False}
            for phase in PHASES
        ],
    }


def default_objective_map(run_id: str) -> dict[str, Any]:
    return {"schema": "objective-map.v1", "run_id": run_id, "objectives": [], "dependencies": []}


def default_team_registry(run_id: str) -> dict[str, Any]:
    return {"schema": "team-registry.v1", "run_id": run_id, "teams": []}


def decompose_goal(project_root: Path, run_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    goal_text = read_text(run_dir / "goal.md")
    objective_descriptions = _extract_objectives(goal_text)
    objectives = []
    for index, description in enumerate(objective_descriptions, start=1):
        objective_id = slugify(description) or f"objective-{index}"
        capabilities = suggest_capabilities(description)
        objectives.append(
            {
                "objective_id": objective_id,
                "title": description,
                "summary": description,
                "status": "proposed",
                "capabilities": capabilities,
            }
        )
    payload = {"schema": "objective-map.v1", "run_id": run_id, "objectives": objectives, "dependencies": []}
    validate_document(payload, "objective-map.v1", project_root)
    write_json(run_dir / "objective-map.json", payload)
    return payload


def suggest_team_proposals(project_root: Path, run_id: str) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    objective_map = read_json(run_dir / "objective-map.json")
    teams = []
    for objective in objective_map["objectives"]:
        team = {
            "objective_id": objective["objective_id"],
            "team_id": f"{objective['objective_id']}-team",
            "status": "proposed",
            "capabilities": objective["capabilities"],
            "roles": suggested_roles(objective["capabilities"]),
        }
        teams.append(team)
    payload = {"schema": "team-registry.v1", "run_id": run_id, "teams": teams}
    validate_document(payload, "team-registry.v1", project_root)
    write_json(run_dir / "team-registry.json", payload)
    proposal_path = run_dir / "team-proposals.json"
    write_json(proposal_path, payload)
    return payload


def generate_role_files(project_root: Path, run_id: str, approve: bool = False) -> list[Path]:
    run_dir = project_root / "runs" / run_id
    registry = read_json(run_dir / "team-registry.json")
    written: list[Path] = []
    for team in registry["teams"]:
        objective_root = find_objective_root(project_root, team["objective_id"], create=True)
        write_text(objective_root / "charter.md", objective_charter(team["objective_id"], team["capabilities"]))
        target_dir = objective_root / ("approved" if approve else "proposed")
        ensure_dir(target_dir)
        for role in team["roles"]:
            capability = role.get("capability")
            content = role_markdown(team["objective_id"], role["role_id"], role["role_kind"], capability)
            path = target_dir / f"{role['role_id'].split('.')[-1]}.md"
            write_text(path, content)
            written.append(path)
    return written


def promote_roles(project_root: Path, objective_id: str) -> list[Path]:
    objective_root = find_objective_root(project_root, objective_id, create=True)
    proposed_dir = objective_root / "proposed"
    approved_dir = ensure_dir(objective_root / "approved")
    copied: list[Path] = []
    for path in proposed_dir.glob("*.md"):
        destination = approved_dir / path.name
        write_text(destination, read_text(path))
        copied.append(destination)
    return copied


def suggest_capabilities(description: str) -> list[str]:
    lowered = description.lower()
    capabilities = [
        name for name, keywords in CAPABILITY_KEYWORDS.items() if any(keyword in lowered for keyword in keywords)
    ]
    return capabilities or ["general"]


def suggested_roles(capabilities: list[str]) -> list[dict[str, Any]]:
    roles = [
        {"role_id": "objective-manager", "role_kind": "manager"},
        {"role_id": "acceptance-manager", "role_kind": "acceptance-manager"},
    ]
    for capability in capabilities:
        if capability == "general":
            roles.append({"role_id": "general-worker", "role_kind": "worker"})
            continue
        roles.append({"role_id": f"{capability}-manager", "role_kind": "manager", "capability": capability})
        roles.append({"role_id": f"{capability}-worker", "role_kind": "worker", "capability": capability})
    return roles


def objective_charter(objective_id: str, capabilities: list[str]) -> str:
    capabilities_text = ", ".join(capabilities)
    return f"""# Objective Charter

Objective: `{objective_id}`

Allowed capabilities: {capabilities_text}

This charter defines the durable boundary for the objective team. The current phase overlay further restricts what this team may do in the active phase.
"""


def role_markdown(objective_id: str, role_id: str, role_kind: str, capability: str | None = None) -> str:
    capability_ref = f"\n- orchestrator/roles/capabilities/{capability}.md" if capability and capability != "general" else ""
    reviewed_by = f"objectives.{objective_id}.acceptance-manager"
    return f"""---
role_id: objectives.{objective_id}.{role_id}
inherits:
  - orchestrator/roles/base/company.md
  - orchestrator/roles/base/{role_kind}.md{capability_ref}
reviewed_by: {reviewed_by}
---

# Mission
Operate as `{role_id}` for objective `{objective_id}`.

# Responsibilities
- Follow the objective charter and current phase overlay.
- Stay within assigned boundaries.
- Use collaboration requests for cross-team or shared-asset needs.

# Completion Rules
- Emit the required runtime contract for your assignment.
"""


def _extract_objectives(goal_text: str) -> list[str]:
    bullets = []
    in_objectives = False
    for raw_line in goal_text.splitlines():
        line = raw_line.strip()
        if re.match(r"^##+\s+Objectives?$", line, re.IGNORECASE):
            in_objectives = True
            continue
        if in_objectives and re.match(r"^##+\s+", line):
            break
        if line.startswith(("-", "*")):
            bullets.append(line[1:].strip())
    if bullets:
        return bullets
    for line in goal_text.splitlines():
        candidate = line.strip()
        if candidate and not candidate.startswith("#"):
            return [candidate]
    return ["default-objective"]


def slugify(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return lowered.strip("-")
