from __future__ import annotations

from pathlib import Path
from typing import Any

from .filesystem import ensure_dir, read_json, read_text, write_json, write_text
from .planner import default_objective_map, default_team_registry
from .schemas import validate_document


def create_change_request(
    project_root: Path,
    run_id: str,
    change_id: str,
    summary: str,
    impact: dict[str, bool],
) -> dict[str, Any]:
    change_dir = ensure_dir(project_root / "runs" / run_id / "changes" / change_id)
    payload = {
        "schema": "change-request.v1",
        "run_id": run_id,
        "change_id": change_id,
        "summary": summary,
        "impact": impact,
        "requested_by": "human",
    }
    validate_document(payload, "change-request.v1", project_root)
    write_json(change_dir / "change-request.json", payload)
    write_text(change_dir / "change-request.md", render_change_request_markdown(payload))
    return payload


def analyze_change_request(project_root: Path, run_id: str, change_id: str) -> dict[str, Any]:
    change_dir = project_root / "runs" / run_id / "changes" / change_id
    request = read_json(change_dir / "change-request.json")
    recommended_phase = determine_reentry_phase(request["impact"])
    payload = {
        "schema": "change-proposal.v1",
        "run_id": run_id,
        "change_id": change_id,
        "summary": request["summary"],
        "recommended_reentry_phase": recommended_phase,
        "impacted_objectives": [],
        "impacted_teams": [],
        "proposed_role_changes": [],
        "regression_scope": "rerun validations for all impacted objectives",
        "approved": False,
    }
    validate_document(payload, "change-proposal.v1", project_root)
    write_json(change_dir / "change-proposal.json", payload)
    write_text(change_dir / "change-proposal.md", render_change_proposal_markdown(payload))
    return payload


def approve_change(project_root: Path, run_id: str, change_id: str, approved: bool) -> dict[str, Any]:
    proposal_path = project_root / "runs" / run_id / "changes" / change_id / "change-proposal.json"
    proposal = read_json(proposal_path)
    proposal["approved"] = approved
    write_json(proposal_path, proposal)
    return proposal


def scaffold_delta_run(project_root: Path, run_id: str, change_id: str) -> Path:
    change_dir = project_root / "runs" / run_id / "changes" / change_id
    proposal = read_json(change_dir / "change-proposal.json")
    if not proposal.get("approved"):
        raise ValueError(f"Change {change_id} has not been approved")
    delta_root = ensure_dir(change_dir / "delta-run")
    for child in [
        "tasks",
        "reports",
        "bundles",
        "collaboration",
        "phase-reports",
        "changes",
        "prompt-logs",
    ]:
        ensure_dir(delta_root / child)
    phase_plan = {
        "schema": "phase-plan.v1",
        "run_id": f"{run_id}:{change_id}",
        "current_phase": proposal["recommended_reentry_phase"],
        "phases": [
            {
                "phase": phase,
                "status": "active" if phase == proposal["recommended_reentry_phase"] else "locked",
                "human_approved": False,
            }
            for phase in ["discovery", "design", "mvp-build", "polish"]
        ],
    }
    write_json(delta_root / "phase-plan.json", phase_plan)
    write_json(delta_root / "objective-map.json", default_objective_map(f"{run_id}:{change_id}"))
    write_json(delta_root / "team-registry.json", default_team_registry(f"{run_id}:{change_id}"))
    goal_source = project_root / "runs" / run_id / "goal.md"
    if goal_source.exists():
        write_text(delta_root / "goal.md", read_text(goal_source))
    return delta_root


def determine_reentry_phase(impact: dict[str, bool]) -> str:
    if impact.get("goal_changed") or impact.get("scope_changed") or impact.get("boundary_changed"):
        return "discovery"
    if impact.get("interface_changed") or impact.get("architecture_changed") or impact.get("team_changed"):
        return "design"
    if impact.get("implementation_changed"):
        return "mvp-build"
    return "polish"


def render_change_request_markdown(payload: dict[str, Any]) -> str:
    return f"""# Change Request

Change id: `{payload['change_id']}`

Summary: {payload['summary']}
"""


def render_change_proposal_markdown(payload: dict[str, Any]) -> str:
    return f"""# Change Proposal

Change id: `{payload['change_id']}`

Summary: {payload['summary']}

Recommended re-entry phase: `{payload['recommended_reentry_phase']}`
"""

