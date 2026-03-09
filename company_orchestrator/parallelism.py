from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from .filesystem import load_optional_json, read_json


def execution_mode(task: dict[str, Any]) -> str:
    return str(task.get("execution_mode", "read_only"))


def parallel_policy(task: dict[str, Any]) -> str:
    return str(task.get("parallel_policy", "serialize"))


def owned_paths(task: dict[str, Any]) -> list[str]:
    return list(task.get("owned_paths", []))


def shared_asset_ids(task: dict[str, Any]) -> list[str]:
    return list(task.get("shared_asset_ids", []))


def warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def infer_execution_metadata(
    *,
    phase: str,
    task_id: str,
    expected_outputs: list[str] | None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(existing or {})
    outputs = list(expected_outputs or [])
    writes_files = any(
        isinstance(item, str)
        and item
        and not item.endswith(".v1")
        and "/" in item
        for item in outputs
    )
    execution_mode = payload.get("execution_mode")
    if execution_mode is None:
        execution_mode = "isolated_write" if writes_files and phase in {"mvp-build", "polish"} else "read_only"
    parallel_policy = payload.get("parallel_policy")
    if parallel_policy is None:
        parallel_policy = "allow" if execution_mode == "read_only" else "serialize"
    owned = payload.get("owned_paths")
    if owned is None:
        owned = outputs if execution_mode == "isolated_write" else []
    shared_assets = payload.get("shared_asset_ids")
    if shared_assets is None:
        shared_assets = []
    return {
        "execution_mode": execution_mode,
        "parallel_policy": parallel_policy,
        "owned_paths": list(owned),
        "shared_asset_ids": list(shared_assets),
    }


def parallel_requested(task: dict[str, Any]) -> bool:
    return parallel_policy(task) == "allow"


def classify_parallel_safety(
    task: dict[str, Any],
    *,
    running_tasks: list[dict[str, Any]],
) -> tuple[bool, str | None, str | None]:
    if not parallel_requested(task):
        return False, "planner_serialize_only", "Planner marked the task as serialize-only."
    mode = execution_mode(task)
    if mode == "read_only":
        return True, None, None
    if mode != "isolated_write":
        return False, "missing_parallel_metadata", "Task execution metadata is incomplete for safe parallel execution."
    task_owned_paths = owned_paths(task)
    if not task_owned_paths:
        return False, "missing_parallel_metadata", "Task is missing owned_paths required for isolated parallel execution."
    for running_task in running_tasks:
        if execution_mode(running_task) == "isolated_write":
            if paths_conflict(task_owned_paths, owned_paths(running_task)):
                return False, "owned_paths_conflict", f"Conflicts on owned paths with {running_task['task_id']}."
            if set(shared_asset_ids(task)) & set(shared_asset_ids(running_task)):
                return False, "shared_asset_conflict", f"Conflicts on shared assets with {running_task['task_id']}."
    return True, None, None


def paths_conflict(left_paths: list[str], right_paths: list[str]) -> bool:
    for left in left_paths:
        for right in right_paths:
            if path_pattern_conflict(left, right):
                return True
    return False


def path_pattern_conflict(left: str, right: str) -> bool:
    if left == right:
        return True
    if "*" in left or "?" in left or "[" in left:
        return fnmatch(right, left) or prefix_overlap(left, right)
    if "*" in right or "?" in right or "[" in right:
        return fnmatch(left, right) or prefix_overlap(left, right)
    return prefix_overlap(left, right)


def prefix_overlap(left: str, right: str) -> bool:
    normalized_left = left.rstrip("/")
    normalized_right = right.rstrip("/")
    return (
        normalized_left == normalized_right
        or normalized_left.startswith(normalized_right + "/")
        or normalized_right.startswith(normalized_left + "/")
    )


def summarize_parallelism_for_phase(run_dir: Path, phase: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = []
    for task in tasks:
        summary = load_optional_json(run_dir / "executions" / f"{task['task_id']}.json")
        if summary is not None:
            summaries.append(summary)
    incidents = []
    tasks_run_in_parallel = 0
    tasks_serialized_by_policy = 0
    tasks_serialized_by_runtime_conflict = 0
    for summary in summaries:
        if summary.get("parallel_execution_granted"):
            tasks_run_in_parallel += 1
        reason = summary.get("parallel_fallback_reason")
        if not reason:
            continue
        warning_codes = {item.get("code") for item in summary.get("runtime_warnings", []) if isinstance(item, dict)}
        if "planner_serialize_only" in warning_codes:
            tasks_serialized_by_policy += 1
        else:
            tasks_serialized_by_runtime_conflict += 1
        incidents.append(
            {
                "task_id": summary["task_id"],
                "reason": reason,
                "artifact_path": summary.get("report_path") or str(Path("runs") / run_dir.name / "reports" / f"{summary['task_id']}.json"),
            }
        )
    return {
        "total_tasks_considered": len(tasks),
        "tasks_run_in_parallel": tasks_run_in_parallel,
        "tasks_serialized_by_policy": tasks_serialized_by_policy,
        "tasks_serialized_by_runtime_conflict": tasks_serialized_by_runtime_conflict,
        "incidents": incidents,
    }
