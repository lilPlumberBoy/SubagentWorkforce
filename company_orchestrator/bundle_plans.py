from __future__ import annotations

from pathlib import Path
from typing import Any

from .filesystem import read_json


def objective_plan_has_no_phase_work(run_dir: Path, phase: str, objective_id: str) -> bool:
    plan_path = run_dir / "manager-plans" / f"{phase}-{objective_id}.json"
    if not plan_path.exists():
        return False
    plan = read_json(plan_path)
    return not plan.get("tasks") and not plan.get("bundle_plan")


def objective_bundle_specs(run_dir: Path, phase: str, objective_id: str, task_ids: list[str]) -> list[dict[str, Any]]:
    plan_path = run_dir / "manager-plans" / f"{phase}-{objective_id}.json"
    if not plan_path.exists():
        if not task_ids:
            return []
        return [{"bundle_id": f"{phase}-{objective_id}-bundle", "task_ids": task_ids, "summary": "default bundle"}]
    plan = read_json(plan_path)
    bundle_plan = plan.get("bundle_plan", [])
    if not bundle_plan:
        if not task_ids and not plan.get("tasks"):
            return []
        return [{"bundle_id": f"{phase}-{objective_id}-bundle", "task_ids": task_ids, "summary": "default bundle"}]
    return bundle_plan
