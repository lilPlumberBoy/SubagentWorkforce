from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from .filesystem import load_optional_json, read_json
from .output_descriptors import normalize_output_descriptors, output_descriptor_paths, split_legacy_asset_descriptor

REPO_PATH_PATTERN = re.compile(r"^(?:\./)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.*?@:-]+)+/?$")


def execution_mode(task: dict[str, Any]) -> str:
    return str(task.get("execution_mode", "read_only"))


def parallel_policy(task: dict[str, Any]) -> str:
    return str(task.get("parallel_policy", "serialize"))


def owned_paths(task: dict[str, Any]) -> list[str]:
    return list(task.get("owned_paths", []))


def writes_existing_paths(task: dict[str, Any]) -> list[str]:
    return list(task.get("writes_existing_paths", []))


def shared_asset_ids(task: dict[str, Any]) -> list[str]:
    return list(task.get("shared_asset_ids", []))


def task_requires_write_access(task: dict[str, Any]) -> bool:
    if execution_mode(task) == "isolated_write":
        return True
    if any(isinstance(item, str) and item for item in owned_paths(task)):
        return True
    if any(isinstance(item, str) and item for item in writes_existing_paths(task)):
        return True
    return bool(concrete_expected_output_paths(task))


def effective_sandbox_mode(task: dict[str, Any], default_sandbox_mode: str) -> str:
    explicit = task.get("sandbox_mode")
    if isinstance(explicit, str) and explicit and explicit != "read-only":
        return explicit
    if task_requires_write_access(task):
        if default_sandbox_mode != "read-only":
            return default_sandbox_mode
        return "workspace-write"
    return explicit or default_sandbox_mode


def split_output_descriptor(value: str) -> tuple[str | None, str | None]:
    return split_legacy_asset_descriptor(value)


def normalize_expected_outputs(values: list[Any]) -> list[dict[str, Any]]:
    return normalize_output_descriptors(values)


def normalize_owned_paths_values(values: list[Any]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        if isinstance(value, dict):
            path = value.get("path")
            if isinstance(path, str) and path.strip():
                normalized.append(path.strip())
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        _, output_path = split_output_descriptor(value)
        normalized.append(output_path or value)
    return dedupe_strings(normalized)


def normalize_task_artifact_descriptors(task: dict[str, Any]) -> None:
    task["expected_outputs"] = normalize_expected_outputs(
        sanitize_expected_outputs(
            list(task.get("expected_outputs", [])),
            shared_asset_ids(task),
        )
    )
    task["owned_paths"] = normalize_owned_paths_values(list(task.get("owned_paths", [])))
    task["writes_existing_paths"] = normalize_owned_paths_values(list(task.get("writes_existing_paths", [])))


def sanitize_expected_outputs(values: list[Any], shared_assets: list[str]) -> list[Any]:
    shared_asset_set = {item.strip() for item in shared_assets if isinstance(item, str) and item.strip()}
    sanitized: list[Any] = []
    for value in values:
        if not isinstance(value, dict):
            sanitized.append(value)
            continue
        kind = str(value.get("kind", "") or "").strip()
        asset_id = str(value.get("asset_id", "") or "").strip()
        if kind == "asset" and asset_id in shared_asset_set and output_path_is_missing(value.get("path")):
            continue
        sanitized.append(value)
    return sanitized


def output_path_is_missing(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return not normalized or normalized in {"none", "null"}


def canonicalize_validation_commands(task: dict[str, Any]) -> None:
    canonical_paths = dedupe_strings(
        [
            item
            for item in concrete_expected_output_paths(task) + [value for value in owned_paths(task) if isinstance(value, str)]
            if isinstance(item, str) and item and "*" not in item and "?" not in item and "[" not in item
        ]
    )
    if not canonical_paths:
        return
    for validation in task.get("validation", []):
        command = validation.get("command")
        if not isinstance(command, str) or not command:
            continue
        updated_command = command
        for canonical_path in canonical_paths:
            if canonical_path in updated_command:
                continue
            replacement = longest_suffix_match(updated_command, canonical_path)
            if replacement is None:
                continue
            updated_command = updated_command.replace(replacement, canonical_path)
        validation["command"] = updated_command


def concrete_expected_output_paths(task: dict[str, Any]) -> list[str]:
    return concrete_expected_output_paths_from_values(task.get("expected_outputs", []))


def concrete_expected_output_paths_from_values(values: list[Any] | None) -> list[str]:
    return dedupe_strings(output_descriptor_paths(values))


def looks_like_repo_path(value: str) -> bool:
    normalized = value.strip()
    if not normalized or " " in normalized:
        return False
    return REPO_PATH_PATTERN.match(normalized) is not None


def longest_suffix_match(command: str, canonical_path: str) -> str | None:
    segments = [segment for segment in canonical_path.split("/") if segment]
    for start in range(1, len(segments)):
        candidate = "/".join(segments[start:])
        if candidate in command:
            return candidate
    if segments:
        basename = segments[-1]
        dotted_basename = f"./{basename}"
        if dotted_basename in command:
            return dotted_basename
        if "." in basename and basename in command:
            return basename
    return None


def warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def infer_execution_metadata(
    *,
    phase: str,
    task_id: str,
    expected_outputs: list[Any] | None,
    writes_existing_paths: list[str] | None = None,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(existing or {})
    outputs = list(expected_outputs or [])
    concrete_outputs = concrete_expected_output_paths_from_values(outputs)
    existing_write_paths = [item for item in list(writes_existing_paths or []) if isinstance(item, str) and item]
    writes_files = bool(concrete_outputs or existing_write_paths)
    execution_mode = payload.get("execution_mode")
    if execution_mode is None:
        execution_mode = "isolated_write" if writes_files else "read_only"
    parallel_policy = payload.get("parallel_policy")
    if parallel_policy is None:
        parallel_policy = "allow" if execution_mode == "read_only" else "serialize"
    owned = payload.get("owned_paths")
    if owned is None:
        owned = dedupe_strings(concrete_outputs + existing_write_paths) if execution_mode == "isolated_write" else []
    shared_assets = payload.get("shared_asset_ids")
    if shared_assets is None:
        shared_assets = []
    return {
        "execution_mode": execution_mode,
        "parallel_policy": parallel_policy,
        "owned_paths": list(owned),
        "writes_existing_paths": existing_write_paths,
        "shared_asset_ids": list(shared_assets),
    }


def parallel_requested(task: dict[str, Any]) -> bool:
    return parallel_policy(task) == "allow"


def dedupe_strings(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def classify_parallel_safety(
    task: dict[str, Any],
    *,
    running_tasks: list[dict[str, Any]],
) -> tuple[bool, str | None, str | None]:
    mode = execution_mode(task)
    if not parallel_requested(task):
        if any(task.get("objective_id") == running_task.get("objective_id") for running_task in running_tasks):
            return False, "planner_serialize_only", "Planner marked the task as serialize-only."
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
