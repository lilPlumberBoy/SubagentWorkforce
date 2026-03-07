from __future__ import annotations

from pathlib import Path
from typing import Any

from .filesystem import read_json


def objective_bundle_specs(run_dir: Path, phase: str, objective_id: str, task_ids: list[str]) -> list[dict[str, Any]]:
    plan_path = run_dir / "manager-plans" / f"{phase}-{objective_id}.json"
    if not plan_path.exists():
        return [{"bundle_id": f"{phase}-{objective_id}-bundle", "task_ids": task_ids, "summary": "default bundle"}]
    plan = read_json(plan_path)
    bundle_plan = plan.get("bundle_plan", [])
    if not bundle_plan:
        return [{"bundle_id": f"{phase}-{objective_id}-bundle", "task_ids": task_ids, "summary": "default bundle"}]
    return bundle_plan

