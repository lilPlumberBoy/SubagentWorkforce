from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .filesystem import append_jsonl, load_optional_json, read_json, write_json_atomic
from .live import list_activities, now_timestamp, process_alive, timestamp_diff_ms
from .schemas import validate_document


def prompt_metrics(prompt_text: str) -> dict[str, int]:
    return {
        "prompt_char_count": len(prompt_text),
        "prompt_line_count": len(prompt_text.splitlines()),
        "prompt_bytes": len(prompt_text.encode("utf-8")),
    }


def record_llm_call(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    activity_id: str,
    kind: str,
    attempt: int,
    started_at: str,
    completed_at: str,
    latency_ms: int,
    queue_wait_ms: int,
    prompt_char_count: int,
    prompt_line_count: int,
    prompt_bytes: int,
    timed_out: bool,
    retry_scheduled: bool,
    success: bool,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    stdout_bytes: int,
    stderr_bytes: int,
    timeout_seconds: int,
    error: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    payload = {
        "schema": "llm-call.v1",
        "run_id": run_id,
        "phase": phase,
        "activity_id": activity_id,
        "kind": kind,
        "attempt": attempt,
        "started_at": started_at,
        "completed_at": completed_at,
        "latency_ms": latency_ms,
        "queue_wait_ms": queue_wait_ms,
        "prompt_char_count": prompt_char_count,
        "prompt_line_count": prompt_line_count,
        "prompt_bytes": prompt_bytes,
        "timed_out": timed_out,
        "retry_scheduled": retry_scheduled,
        "success": success,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "timeout_seconds": timeout_seconds,
        "error": error,
        "label": label,
    }
    validate_document(payload, "llm-call.v1", project_root)
    append_jsonl(run_dir / "live" / "llm-calls.jsonl", payload)
    refresh_run_observability(project_root, run_id)
    return payload


def read_llm_calls(
    project_root: Path,
    run_id: str,
    *,
    phase: str | None = None,
    activity_id: str | None = None,
) -> list[dict[str, Any]]:
    path = project_root / "runs" / run_id / "live" / "llm-calls.jsonl"
    if not path.exists():
        return []
    calls: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        payload = read_json_line(stripped)
        if phase is not None and payload["phase"] != phase:
            continue
        if activity_id is not None and payload["activity_id"] != activity_id:
            continue
        calls.append(payload)
    return calls


def refresh_run_observability(project_root: Path, run_id: str) -> dict[str, Any]:
    calls = read_llm_calls(project_root, run_id)
    summary = summarize_calls(calls)
    activities = list_activities(project_root, run_id)
    active_activities = [
        activity
        for activity in activities
        if process_alive(activity.get("process_metadata"))
    ]
    active_processes = len(active_activities)
    active_stream_stdout_bytes = sum(
        int((activity.get("observability", {}) or {}).get("stream_stdout_bytes", 0))
        for activity in active_activities
    )
    active_stream_stderr_bytes = sum(
        int((activity.get("observability", {}) or {}).get("stream_stderr_bytes", 0))
        for activity in active_activities
    )
    max_active_runtime_ms = max(
        (
            int((activity.get("observability", {}) or {}).get("runtime_ms", 0))
            for activity in active_activities
        ),
        default=0,
    )
    max_last_signal_age_ms = max(
        (
            timestamp_diff_ms((activity.get("observability", {}) or {}).get("last_signal_at"), now_timestamp())
            for activity in active_activities
            if (activity.get("observability", {}) or {}).get("last_signal_at")
        ),
        default=0,
    )
    active_calls_by_kind = dict(Counter(activity["kind"] for activity in active_activities))
    payload = {
        "schema": "run-observability.v1",
        "run_id": run_id,
        "total_calls": summary["total_calls"],
        "completed_calls": summary["completed_calls"],
        "failed_calls": summary["failed_calls"],
        "timed_out_calls": summary["timed_out_calls"],
        "retry_scheduled_calls": summary["retry_scheduled_calls"],
        "total_input_tokens": summary["total_input_tokens"],
        "total_cached_input_tokens": summary["total_cached_input_tokens"],
        "total_output_tokens": summary["total_output_tokens"],
        "total_prompt_chars": summary["total_prompt_chars"],
        "total_prompt_lines": summary["total_prompt_lines"],
        "average_latency_ms": summary["average_latency_ms"],
        "max_latency_ms": summary["max_latency_ms"],
        "average_queue_wait_ms": summary["average_queue_wait_ms"],
        "active_processes": active_processes,
        "active_stream_stdout_bytes": active_stream_stdout_bytes,
        "active_stream_stderr_bytes": active_stream_stderr_bytes,
        "max_active_runtime_ms": max_active_runtime_ms,
        "max_last_signal_age_ms": max_last_signal_age_ms,
        "active_calls_by_kind": active_calls_by_kind,
        "calls_by_kind": summary["calls_by_kind"],
        "updated_at": now_timestamp(),
    }
    validate_document(payload, "run-observability.v1", project_root)
    write_json_atomic(project_root / "runs" / run_id / "live" / "observability.json", payload)
    return payload


def read_run_observability(project_root: Path, run_id: str) -> dict[str, Any]:
    path = project_root / "runs" / run_id / "live" / "observability.json"
    payload = load_optional_json(path)
    if payload is not None:
        return payload
    return refresh_run_observability(project_root, run_id)


def summarize_observability_for_phase(project_root: Path, run_id: str, phase: str) -> dict[str, Any]:
    summary = summarize_calls(read_llm_calls(project_root, run_id, phase=phase))
    summary.pop("calls_by_kind", None)
    return summary


def planning_compaction_profile(project_root: Path, run_id: str, phase: str) -> dict[str, Any]:
    planning_calls = [
        call
        for call in read_llm_calls(project_root, run_id, phase=phase)
        if call.get("kind") in {"objective_plan", "capability_plan"}
    ]
    if not planning_calls:
        return {
            "level": "compact",
            "reason": "No prior planning observability was available for this phase; defaulting to compact payloads.",
            "limits": {
                "existing_tasks": 4,
                "prior_reports": 2,
                "prior_artifacts": 3,
                "catalog_reports": 4,
                "catalog_artifacts": 4,
                "outline_edges": 4,
                "objective_details": 3,
                "section_max_length": 300,
                "detail_max_length": 380,
                "outline_summary_max_length": 200,
                "dependency_note_limit": 4,
                "dependency_note_max_length": 120,
            },
        }
    summary = summarize_calls(planning_calls)
    max_prompt_chars = max(int(call["prompt_char_count"]) for call in planning_calls)
    max_latency_ms = max(int(call["latency_ms"]) for call in planning_calls)
    timed_out = any(bool(call["timed_out"]) for call in planning_calls)
    retry_scheduled = any(bool(call["retry_scheduled"]) for call in planning_calls)
    if timed_out or retry_scheduled or max_prompt_chars >= 20000 or max_latency_ms >= 240000:
        return {
            "level": "aggressive",
            "reason": "Recent planning calls were large, slow, or timed out; using aggressive compaction.",
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
    if summary["average_latency_ms"] >= 120000 or max_prompt_chars >= 12000:
        return {
            "level": "compact",
            "reason": "Recent planning calls were moderately large or slow; using compact payloads.",
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
    return {
        "level": "standard",
        "reason": "Recent planning calls were within normal size and latency bounds.",
        "limits": {
            "existing_tasks": 8,
            "prior_reports": 6,
            "prior_artifacts": 8,
            "catalog_reports": 12,
            "catalog_artifacts": 12,
            "outline_edges": 8,
            "objective_details": 6,
            "section_max_length": 700,
            "detail_max_length": 900,
            "outline_summary_max_length": 360,
            "dependency_note_limit": 8,
            "dependency_note_max_length": 200,
        },
    }


def recommend_runtime_tuning(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    action_kind: str,
    requested_max_concurrency: int,
) -> dict[str, Any]:
    if action_kind == "planning":
        relevant_kinds = {"objective_plan", "capability_plan"}
    else:
        relevant_kinds = {"task_execution"}
    calls = [
        call
        for call in read_llm_calls(project_root, run_id, phase=phase)
        if call.get("kind") in relevant_kinds
    ]
    effective_max = max(1, int(requested_max_concurrency))
    if not calls:
        if action_kind == "planning":
            objective_map = load_optional_json(project_root / "runs" / run_id / "objective-map.json") or {"objectives": []}
            objective_count = len(objective_map.get("objectives", []))
            goal_path = project_root / "runs" / run_id / "goal.md"
            goal_chars = len(goal_path.read_text(encoding="utf-8")) if goal_path.exists() else 0
            if objective_count >= 5 or goal_chars >= 8000:
                effective_max = 1
                reason = "Cold-start planning heuristic reduced concurrency because the run has many objectives or a very large goal."
            elif objective_count >= 4 or goal_chars >= 5000:
                effective_max = min(effective_max, 2)
                reason = "Cold-start planning heuristic reduced concurrency for a larger goal before observability was available."
            else:
                reason = "No prior observability was available for this phase and action type."
        else:
            tasks_dir = project_root / "runs" / run_id / "tasks"
            isolated_write_count = 0
            if tasks_dir.exists():
                for path in tasks_dir.glob("*.json"):
                    payload = load_optional_json(path)
                    if payload and payload.get("phase") == phase and payload.get("execution_mode") == "isolated_write":
                        isolated_write_count += 1
            if isolated_write_count >= 4:
                effective_max = min(effective_max, 2)
                reason = "Cold-start execution heuristic reduced concurrency because this phase contains multiple isolated-write tasks."
            else:
                reason = "No prior observability was available for this phase and action type."
        return {
            "action_kind": action_kind,
            "requested_max_concurrency": requested_max_concurrency,
            "effective_max_concurrency": effective_max,
            "reason": reason,
            "observed_calls": 0,
            "timed_out_calls": 0,
            "retry_scheduled_calls": 0,
            "average_latency_ms": 0,
        }
    summary = summarize_calls(calls)
    timed_out_calls = int(summary["timed_out_calls"])
    retry_scheduled_calls = int(summary["retry_scheduled_calls"])
    average_latency_ms = int(summary["average_latency_ms"])
    reason = "No adaptive tuning was needed."
    if action_kind == "planning":
        if timed_out_calls >= 2:
            effective_max = 1
            reason = "Reduced planning concurrency to 1 after repeated planning timeouts."
        elif timed_out_calls >= 1 or retry_scheduled_calls >= 1:
            effective_max = min(effective_max, 2)
            reason = "Reduced planning concurrency after timeout/retry pressure in this phase."
        elif average_latency_ms >= 180000:
            effective_max = min(effective_max, 2)
            reason = "Reduced planning concurrency after slow recent planning calls."
    else:
        if timed_out_calls >= 2:
            effective_max = 1
            reason = "Reduced execution concurrency to 1 after repeated task timeouts."
        elif timed_out_calls >= 1 or retry_scheduled_calls >= 2:
            effective_max = min(effective_max, 2)
            reason = "Reduced execution concurrency after timeout/retry pressure in this phase."
        elif average_latency_ms >= 240000:
            effective_max = min(effective_max, 2)
            reason = "Reduced execution concurrency after slow recent task calls."
    return {
        "action_kind": action_kind,
        "requested_max_concurrency": requested_max_concurrency,
        "effective_max_concurrency": effective_max,
        "reason": reason,
        "observed_calls": summary["total_calls"],
        "timed_out_calls": timed_out_calls,
        "retry_scheduled_calls": retry_scheduled_calls,
        "average_latency_ms": average_latency_ms,
    }


def compact_observability_for_report(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if payload is None:
        return None
    allowed_keys = {
        "prompt_char_count",
        "prompt_line_count",
        "prompt_bytes",
        "queue_wait_ms",
        "runtime_ms",
        "last_call_latency_ms",
        "llm_call_count",
        "timeout_count",
        "timeout_retry_count",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "stdout_bytes",
        "stderr_bytes",
    }
    return {key: payload.get(key, 0) for key in allowed_keys}


def summarize_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    total_calls = len(calls)
    completed_calls = sum(1 for call in calls if call["success"] and not call["timed_out"])
    failed_calls = sum(1 for call in calls if not call["success"] and not call["timed_out"])
    timed_out_calls = sum(1 for call in calls if call["timed_out"])
    retry_scheduled_calls = sum(1 for call in calls if call["retry_scheduled"])
    total_input_tokens = sum(int(call["input_tokens"]) for call in calls)
    total_cached_input_tokens = sum(int(call["cached_input_tokens"]) for call in calls)
    total_output_tokens = sum(int(call["output_tokens"]) for call in calls)
    total_prompt_chars = sum(int(call["prompt_char_count"]) for call in calls)
    total_prompt_lines = sum(int(call["prompt_line_count"]) for call in calls)
    average_latency_ms = int(sum(int(call["latency_ms"]) for call in calls) / total_calls) if total_calls else 0
    max_latency_ms = max((int(call["latency_ms"]) for call in calls), default=0)
    average_queue_wait_ms = int(sum(int(call["queue_wait_ms"]) for call in calls) / total_calls) if total_calls else 0
    calls_by_kind = dict(Counter(call["kind"] for call in calls))
    return {
        "total_calls": total_calls,
        "completed_calls": completed_calls,
        "failed_calls": failed_calls,
        "timed_out_calls": timed_out_calls,
        "retry_scheduled_calls": retry_scheduled_calls,
        "total_input_tokens": total_input_tokens,
        "total_cached_input_tokens": total_cached_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_prompt_chars": total_prompt_chars,
        "total_prompt_lines": total_prompt_lines,
        "average_latency_ms": average_latency_ms,
        "max_latency_ms": max_latency_ms,
        "average_queue_wait_ms": average_queue_wait_ms,
        "calls_by_kind": calls_by_kind,
    }


def read_json_line(line: str) -> dict[str, Any]:
    import json

    return json.loads(line)
