from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .autonomy import autonomy_history_path, read_autonomy_state
from .filesystem import load_optional_json, read_json, read_text
from .handoffs import list_handoffs
from .live import (
    TERMINAL_ACTIVITY_STATUSES,
    list_activities,
    read_activity,
    read_activity_history,
    read_events,
    read_run_state,
    refresh_run_state,
)
from .management import run_guidance
from .monitoring import (
    activity_code,
    activity_label,
    activity_observability_summary,
    activity_sort_key,
    age_text,
    build_objective_lookup,
    elapsed_text,
    history_activity_label,
    humanize_ms,
    is_active,
    last_event_age_text,
    load_prompt_text,
    objective_label,
    objective_progress_fraction,
    parse_timestamp,
    status_label,
    summarize_objective_statuses,
)
from .observability import read_run_observability


class MonitorApiError(Exception):
    status = HTTPStatus.INTERNAL_SERVER_ERROR


class MonitorNotFoundError(MonitorApiError):
    status = HTTPStatus.NOT_FOUND


class MonitorBadRequestError(MonitorApiError):
    status = HTTPStatus.BAD_REQUEST


def open_project_path(project_root: Path, raw_path: str) -> dict[str, Any]:
    target_path = resolve_openable_project_path(project_root, raw_path)
    open_local_path(target_path)
    return {
        "opened": True,
        "path": relative_project_path(project_root, target_path),
    }


def list_runs_payload(project_root: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    runs_root = project_root / "runs"
    if runs_root.exists():
        for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
            if not (run_dir / "phase-plan.json").exists():
                continue
            try:
                items.append(build_run_list_item(project_root, run_dir.name))
            except Exception:
                continue
    items.sort(
        key=lambda item: (
            parse_timestamp(item.get("started_at")) or parse_timestamp("1970-01-01T00:00:00Z"),
            item["run_id"],
        ),
        reverse=True,
    )
    return {
        "runs": items,
        "updated_at": max((item["updated_at"] for item in items), default=None),
    }


def build_run_list_item(project_root: Path, run_id: str) -> dict[str, Any]:
    run_state = refresh_run_state(project_root, run_id)
    guidance = run_guidance(project_root, run_id, phase=run_state["current_phase"])
    autonomy_state = read_autonomy_state(project_root, run_id)
    started_at = infer_run_started_at(project_root, run_id)
    return {
        "run_id": run_id,
        "current_phase": run_state["current_phase"],
        "started_at": started_at,
        "updated_at": run_state["updated_at"],
        "run_status": guidance["run_status"],
        "run_status_reason": guidance["run_status_reason"],
        "controller_status": autonomy_state["status"],
        "active_activity_count": len(run_state.get("active_activity_ids", [])),
        "queued_activity_count": len(run_state.get("queued_activity_ids", [])),
    }


def build_run_dashboard_payload(project_root: Path, run_id: str, *, events_limit: int = 50) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    if not run_dir.exists():
        raise MonitorNotFoundError(f"Run {run_id} was not found.")

    refresh_run_state(project_root, run_id)
    run_state = read_run_state(project_root, run_id)
    current_phase = run_state["current_phase"]
    guidance = run_guidance(project_root, run_id, phase=current_phase)
    autonomy_state = read_autonomy_state(project_root, run_id)
    observability = read_run_observability(project_root, run_id)
    activities = list_activities(project_root, run_id, phase=current_phase)
    history = read_activity_history(project_root, run_id)
    handoffs = list_handoffs(run_dir, phase=current_phase)
    events = read_events(project_root, run_id)[-events_limit:]
    objective_map = load_optional_json(run_dir / "objective-map.json") or {"objectives": []}
    objectives = objective_map.get("objectives", [])
    objective_lookup = build_objective_lookup(objectives)

    active_planning = [
        activity
        for activity in activities
        if activity["kind"] in {"objective_plan", "capability_plan"}
        and activity["status"] not in TERMINAL_ACTIVITY_STATUSES
    ]
    active_tasks = [activity for activity in activities if activity["kind"] == "task_execution" and is_active(activity)]
    queued_tasks = [activity for activity in activities if activity["kind"] == "task_execution" and activity["status"] == "queued"]
    blocked_tasks = [
        activity
        for activity in activities
        if activity["kind"] == "task_execution" and activity["status"] in {"waiting_dependencies", "blocked"}
    ]
    interrupted_or_recovered = [
        activity for activity in activities if activity["status"] in {"interrupted", "recovered", "abandoned"}
    ]

    return {
        "run": {
            "run_id": run_id,
            "current_phase": current_phase,
            "started_at": infer_run_started_at(project_root, run_id),
            "updated_at": run_state["updated_at"],
            "objective_count": len(objectives),
            "active_activity_count": len(run_state.get("active_activity_ids", [])),
            "queued_activity_count": len(run_state.get("queued_activity_ids", [])),
        },
        "guidance": serialize_guidance(guidance),
        "autonomy": serialize_autonomy(project_root, run_id, autonomy_state, guidance),
        "counts": {
            "counts_by_kind": dict(run_state.get("counts_by_kind", {})),
            "counts_by_status": dict(run_state.get("counts_by_status", {})),
        },
        "observability": serialize_observability(observability),
        "objective_progress": [
            {
                "objective_id": objective["objective_id"],
                "label": objective_label(objective["objective_id"], objective_lookup),
                "title": (objective_lookup.get(objective["objective_id"]) or {}).get("title", objective["objective_id"]),
                "phase": current_phase,
                "progress_fraction": objective_progress_fraction(objective["objective_id"], activities),
                "progress_percent": percent_text(objective_progress_fraction(objective["objective_id"], activities)),
                "status_summary": summarize_objective_statuses(objective["objective_id"], activities),
            }
            for objective in objectives
        ],
        "activities": {
            "active_planning": [serialize_activity_row(item, objective_lookup) for item in sorted(active_planning, key=activity_sort_key)],
            "active_tasks": [serialize_activity_row(item, objective_lookup) for item in sorted(active_tasks, key=activity_sort_key)],
            "queued_tasks": [serialize_activity_row(item, objective_lookup) for item in sorted(queued_tasks, key=activity_sort_key)],
            "blocked_tasks": [serialize_activity_row(item, objective_lookup) for item in sorted(blocked_tasks, key=activity_sort_key)],
            "interrupted_or_recovered": [
                serialize_activity_row(item, objective_lookup) for item in sorted(interrupted_or_recovered, key=activity_sort_key)
            ],
        },
        "handoffs": [serialize_handoff_row(item, objective_lookup) for item in handoffs],
        "warnings": serialize_warning_rows(activities, objective_lookup),
        "recovery": serialize_recovery_rows(activities, objective_lookup),
        "history": serialize_history_rows(history, objective_lookup),
        "events": [serialize_event(event) for event in events],
    }


def build_activity_detail_payload(
    project_root: Path,
    run_id: str,
    activity_id: str,
    *,
    events_limit: int = 20,
) -> dict[str, Any]:
    activity = read_activity(project_root, run_id, activity_id)
    objective_lookup = load_objective_lookup(project_root, run_id)
    return {
        "activity": serialize_activity_detail(activity, objective_lookup),
        "artifacts": serialize_artifact_paths(activity),
        "events": [serialize_event(event) for event in read_events(project_root, run_id, activity_id=activity_id)[-events_limit:]],
    }


def build_prompt_debug_payload(
    project_root: Path,
    run_id: str,
    activity_id: str,
    *,
    variant: str | None = None,
) -> dict[str, Any]:
    activity = read_activity(project_root, run_id, activity_id)
    observability = activity.get("observability", {}) or {}
    prompt_artifacts = resolve_prompt_debug_artifacts(project_root, run_id, activity, variant=variant)
    prompt_text = load_prompt_text(project_root, prompt_artifacts["prompt_path"])
    stdout_failure = summarize_stdout_failure(project_root, prompt_artifacts["stdout_path"])
    return {
        "activity_id": activity["activity_id"],
        "kind": activity["kind"],
        "status": activity["status"],
        "attempt": int(activity.get("attempt", 1)),
        "variant": prompt_artifacts["variant"],
        "variant_label": prompt_artifacts["variant_label"],
        "available_variants": prompt_artifacts["available_variants"],
        "prompt_path": prompt_artifacts["prompt_path"],
        "prompt_text": prompt_text,
        "response_path": prompt_artifacts["response_path"],
        "response_text": prompt_artifacts["response_text"],
        "structured_output_path": prompt_artifacts["structured_output_path"],
        "structured_output_text": prompt_artifacts["structured_output_text"],
        "stdout_path": prompt_artifacts["stdout_path"],
        "stderr_path": prompt_artifacts["stderr_path"],
        "stdout_failure": stdout_failure,
        "repair_context": summarize_repair_context(
            activity,
            prompt_artifacts,
            prompt_text,
            stdout_failure=prompt_artifacts.get("original_stdout_failure") or stdout_failure,
        ),
        "observability": {
            "prompt_char_count": int(observability.get("prompt_char_count", 0)),
            "prompt_line_count": int(observability.get("prompt_line_count", 0)),
            "prompt_bytes": int(observability.get("prompt_bytes", 0)),
            "queue_wait_ms": int(observability.get("queue_wait_ms", 0)),
            "time_to_first_stream_ms": int(observability.get("time_to_first_stream_ms", 0)),
            "processing_ms": int(observability.get("processing_ms", 0)),
            "runtime_ms": int(observability.get("runtime_ms", 0)),
            "wall_clock_ms": int(observability.get("wall_clock_ms", 0)),
            "input_tokens": int(observability.get("input_tokens", 0)),
            "cached_input_tokens": int(observability.get("cached_input_tokens", 0)),
            "output_tokens": int(observability.get("output_tokens", 0)),
            "stdout_bytes": int(observability.get("stdout_bytes", 0)),
            "stderr_bytes": int(observability.get("stderr_bytes", 0)),
            "submitted_at": observability.get("submitted_at"),
            "launched_at": observability.get("launched_at"),
            "thread_started_at": observability.get("thread_started_at"),
            "turn_started_at": observability.get("turn_started_at"),
            "first_stream_at": observability.get("first_stream_at"),
            "turn_completed_at": observability.get("turn_completed_at"),
        },
        "artifacts": serialize_artifact_paths(activity),
    }


def build_events_payload(
    project_root: Path,
    run_id: str,
    *,
    limit: int = 50,
    activity_id: str | None = None,
) -> dict[str, Any]:
    return {
        "events": [serialize_event(event) for event in read_events(project_root, run_id, activity_id=activity_id)[-limit:]],
        "limit": limit,
        "run_id": run_id,
    }


def serialize_guidance(guidance: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_status": guidance["run_status"],
        "run_status_reason": guidance["run_status_reason"],
        "next_action_command": guidance.get("next_action_command"),
        "next_action_reason": guidance["next_action_reason"],
        "review_doc_path": guidance.get("review_doc_path"),
        "phase_recommendation": guidance.get("phase_recommendation"),
    }


def serialize_autonomy(
    project_root: Path,
    run_id: str,
    state: dict[str, Any],
    guidance: dict[str, Any],
) -> dict[str, Any]:
    audit_path = autonomy_history_path(project_root, run_id)
    execution_note = None
    if state["status"] == "waiting_for_approval":
        execution_note = "The controller is paused at a human review gate."
    elif state["status"] != "running" and guidance["run_status"] == "working":
        execution_note = "Run work is active outside the autonomous controller."
    elif state["status"] != "running" and guidance["run_status"] == "recoverable":
        execution_note = "The run can continue, but the autonomous controller is not currently attached."
    return {
        "controller_status": state["status"],
        "run_status": guidance["run_status"],
        "auto_approve": bool(state["auto_approve"]),
        "approval_scope": state.get("approval_scope", "all"),
        "stop_before_phases": list(state.get("stop_before_phases", [])),
        "stop_on_recovery": bool(state.get("stop_on_recovery", False)),
        "adaptive_tuning": bool(state.get("adaptive_tuning", True)),
        "sandbox_mode": state["sandbox_mode"],
        "max_concurrency": int(state["max_concurrency"]),
        "timeout_seconds": state.get("timeout_seconds"),
        "active_phase": state.get("active_phase"),
        "last_action": state.get("last_action"),
        "last_action_status": state.get("last_action_status"),
        "stop_reason": state.get("stop_reason"),
        "last_tuning_decision": state.get("last_tuning_decision"),
        "audit_log_path": relative_project_path(project_root, audit_path) if audit_path.exists() else None,
        "execution_note": execution_note,
    }


def serialize_observability(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_calls": int(payload["total_calls"]),
        "completed_calls": int(payload["completed_calls"]),
        "failed_calls": int(payload["failed_calls"]),
        "timed_out_calls": int(payload["timed_out_calls"]),
        "retry_scheduled_calls": int(payload["retry_scheduled_calls"]),
        "total_input_tokens": int(payload["total_input_tokens"]),
        "total_cached_input_tokens": int(payload["total_cached_input_tokens"]),
        "total_output_tokens": int(payload["total_output_tokens"]),
        "total_prompt_chars": int(payload["total_prompt_chars"]),
        "total_prompt_lines": int(payload["total_prompt_lines"]),
        "average_latency_ms": int(payload["average_latency_ms"]),
        "max_latency_ms": int(payload["max_latency_ms"]),
        "average_queue_wait_ms": int(payload["average_queue_wait_ms"]),
        "active_processes": int(payload["active_processes"]),
        "active_stream_stdout_bytes": int(payload["active_stream_stdout_bytes"]),
        "active_stream_stderr_bytes": int(payload["active_stream_stderr_bytes"]),
        "max_active_runtime_ms": int(payload["max_active_runtime_ms"]),
        "max_last_signal_age_ms": int(payload["max_last_signal_age_ms"]),
        "active_calls_by_kind": dict(payload.get("active_calls_by_kind", {})),
        "calls_by_kind": dict(payload.get("calls_by_kind", {})),
        "updated_at": payload["updated_at"],
        "latency_summary": {
            "average": humanize_ms(int(payload["average_latency_ms"])),
            "max": humanize_ms(int(payload["max_latency_ms"])),
            "queue_wait_average": humanize_ms(int(payload["average_queue_wait_ms"])),
        },
    }


def serialize_activity_row(activity: dict[str, Any], objective_lookup: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {
        "activity_id": activity["activity_id"],
        "activity_code": activity_code(activity),
        "label": activity_label(activity),
        "display_name": activity.get("display_name") or activity["activity_id"],
        "objective_id": activity["objective_id"],
        "objective_label": objective_label(activity["objective_id"], objective_lookup),
        "kind": activity["kind"],
        "phase": activity["phase"],
        "status": activity["status"],
        "status_label": status_label(activity),
        "attempt": int(activity.get("attempt", 1)),
        "progress_fraction": float(activity["progress_fraction"]),
        "progress_percent": percent_text(float(activity["progress_fraction"])),
        "current_activity": activity.get("current_activity"),
        "llm_summary": activity_observability_summary(activity),
        "warnings": list(activity.get("warnings", [])),
        "warnings_text": "; ".join(item["message"] for item in activity.get("warnings", [])) or None,
        "elapsed": elapsed_text(activity),
        "last_event_age": last_event_age_text(activity),
        "latest_event": activity.get("latest_event"),
        "queue_position": activity.get("queue_position"),
        "updated_at": activity["updated_at"],
    }


def serialize_activity_detail(activity: dict[str, Any], objective_lookup: dict[str, dict[str, str]]) -> dict[str, Any]:
    payload = dict(activity)
    payload["activity_code"] = activity_code(activity)
    payload["label"] = activity_label(activity)
    payload["objective_label"] = objective_label(activity["objective_id"], objective_lookup)
    payload["status_label"] = status_label(activity)
    payload["elapsed"] = elapsed_text(activity)
    payload["last_event_age"] = last_event_age_text(activity)
    return payload


def serialize_artifact_paths(activity: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt_path": activity.get("prompt_path"),
        "stdout_path": activity.get("stdout_path"),
        "stderr_path": activity.get("stderr_path"),
        "output_path": activity.get("output_path"),
        "workspace_path": activity.get("workspace_path"),
        "branch_name": activity.get("branch_name"),
    }


def serialize_handoff_row(handoff: dict[str, Any], objective_lookup: dict[str, dict[str, str]]) -> dict[str, Any]:
    return {
        "handoff_id": handoff["handoff_id"],
        "objective_id": handoff["objective_id"],
        "objective_label": objective_label(handoff["objective_id"], objective_lookup),
        "status": handoff["status"],
        "status_reason": handoff.get("status_reason"),
        "from_task_id": handoff["from_task_id"],
        "to_task_ids": list(handoff.get("to_task_ids", [])),
        "blocking": bool(handoff.get("blocking", False)),
    }


def serialize_warning_rows(activities: list[dict[str, Any]], objective_lookup: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for activity in sorted(activities, key=activity_sort_key):
        for warning in activity.get("warnings", []):
            rows.append(
                {
                    "activity_id": activity["activity_id"],
                    "activity_label": activity_label(activity),
                    "objective_id": activity["objective_id"],
                    "objective_label": objective_label(activity["objective_id"], objective_lookup),
                    "code": warning["code"],
                    "message": warning["message"],
                }
            )
    return rows


def serialize_recovery_rows(activities: list[dict[str, Any]], objective_lookup: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for activity in sorted(activities, key=activity_sort_key):
        if activity["status"] not in {"interrupted", "recovered", "abandoned"}:
            continue
        rows.append(
            {
                "activity_id": activity["activity_id"],
                "label": activity_label(activity),
                "objective_id": activity["objective_id"],
                "objective_label": objective_label(activity["objective_id"], objective_lookup),
                "status": activity["status"],
                "status_reason": activity.get("status_reason"),
                "recovery_action": activity.get("recovery_action"),
            }
        )
    return rows


def serialize_history_rows(history: list[dict[str, Any]], objective_lookup: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    rows = []
    for entry in select_recent_history_entries(history):
        rows.append(
            {
                "activity_id": entry["activity_id"],
                "label": history_activity_label(entry),
                "objective_id": entry["objective_id"],
                "objective_label": objective_label(entry["objective_id"], objective_lookup),
                "phase": entry.get("phase"),
                "status": entry["status"],
                "timestamp": entry["timestamp"],
                "attempt": int(entry.get("attempt", 1)),
                "status_reason": entry.get("status_reason"),
                "recovery_action": entry.get("recovery_action"),
                "current_activity": entry.get("current_activity"),
            }
        )
    return rows


def select_recent_history_entries(history: list[dict[str, Any]], *, groups_per_phase: int = 4) -> list[dict[str, Any]]:
    if not history:
        return []

    latest_by_phase_and_activity: dict[tuple[str, str], str] = {}
    entries_by_phase_and_activity: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for entry in history:
        phase = str(entry.get("phase") or "unknown")
        activity_id = str(entry.get("activity_id") or "")
        key = (phase, activity_id)
        entries_by_phase_and_activity.setdefault(key, []).append(entry)
        timestamp = str(entry.get("timestamp") or "")
        latest = latest_by_phase_and_activity.get(key)
        if latest is None or timestamp > latest:
            latest_by_phase_and_activity[key] = timestamp

    selected_keys: set[tuple[str, str]] = set()
    phases_by_recency: dict[str, str] = {}
    for (phase, activity_id), latest_timestamp in latest_by_phase_and_activity.items():
        phases_by_recency[phase] = max(latest_timestamp, phases_by_recency.get(phase, ""))

    for phase, _latest_timestamp in sorted(
        phases_by_recency.items(),
        key=lambda item: item[1],
        reverse=True,
    ):
        activity_keys = [
            key for key in latest_by_phase_and_activity.keys() if key[0] == phase
        ]
        activity_keys.sort(
            key=lambda key: latest_by_phase_and_activity[key],
            reverse=True,
        )
        selected_keys.update(activity_keys[:groups_per_phase])

    selected_entries = [
        entry
        for entry in history
        if (str(entry.get("phase") or "unknown"), str(entry.get("activity_id") or "")) in selected_keys
    ]
    return sorted(selected_entries, key=lambda item: item["timestamp"], reverse=True)


def serialize_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": event["timestamp"],
        "activity_id": event.get("activity_id"),
        "event_type": event["event_type"],
        "message": event["message"],
    }


def load_objective_lookup(project_root: Path, run_id: str) -> dict[str, dict[str, str]]:
    objective_map = load_optional_json(project_root / "runs" / run_id / "objective-map.json") or {"objectives": []}
    return build_objective_lookup(objective_map.get("objectives", []))


def percent_text(fraction: float) -> str:
    clamped = max(0.0, min(1.0, fraction))
    return f"{int(clamped * 100)}%"


def relative_project_path(project_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)


def resolve_prompt_debug_artifacts(
    project_root: Path,
    run_id: str,
    activity: dict[str, Any],
    *,
    variant: str | None,
) -> dict[str, Any]:
    summary = load_activity_summary(project_root, run_id, activity)
    current_artifacts = resolve_variant_artifacts(project_root, activity, summary, variant="current")
    original_artifacts = resolve_variant_artifacts(project_root, activity, summary, variant="original")
    selected_variant = "original" if variant == "original" and original_artifacts is not None else "current"
    selected_artifacts = original_artifacts if selected_variant == "original" and original_artifacts is not None else current_artifacts
    available_variants = ["current"]
    if original_artifacts is not None:
        available_variants.insert(0, "original")
    return {
        "available_variants": available_variants,
        "is_repair": original_artifacts is not None or int(activity.get("attempt", 1)) > 1 or bool(activity.get("recovery_action")),
        "prompt_path": selected_artifacts["prompt_path"],
        "response_path": selected_artifacts["response_path"],
        "response_text": load_artifact_text(project_root, selected_artifacts["response_path"]),
        "structured_output_path": selected_artifacts["structured_output_path"],
        "structured_output_text": load_artifact_text(project_root, selected_artifacts["structured_output_path"]),
        "stdout_path": selected_artifacts["stdout_path"],
        "stderr_path": selected_artifacts["stderr_path"],
        "original_stdout_failure": summarize_stdout_failure(
            project_root,
            original_artifacts["stdout_path"] if original_artifacts is not None else None,
        ),
        "variant": selected_variant,
        "variant_label": "Recovery attempt" if selected_variant == "current" and original_artifacts is not None else ("Original attempt" if selected_variant == "original" else "Current attempt"),
    }


def summarize_repair_context(
    activity: dict[str, Any],
    prompt_artifacts: dict[str, Any],
    prompt_text: str | None,
    *,
    stdout_failure: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    is_repair = bool(prompt_artifacts.get("is_repair"))
    if not is_repair:
        return None

    failure_summary = summarize_repair_failure(activity, prompt_text, stdout_failure=stdout_failure)
    repair_request_summary = summarize_repair_request(prompt_text, activity)
    recovery_action = activity.get("recovery_action")

    return {
        "failure_summary": failure_summary,
        "failure_command": (stdout_failure or {}).get("command"),
        "failure_excerpt": (stdout_failure or {}).get("excerpt"),
        "failure_stdout_path": (stdout_failure or {}).get("stdout_path"),
        "is_repair": True,
        "repair_request_summary": repair_request_summary,
        "recovery_action": humanize_symbolic_text(recovery_action) if recovery_action else None,
    }


def summarize_repair_failure(
    activity: dict[str, Any],
    prompt_text: str | None,
    *,
    stdout_failure: dict[str, Any] | None = None,
) -> str:
    section = extract_markdown_section(prompt_text, "What Failed In The Previous Response")
    lines = normalized_section_lines(section)
    for line in lines:
        if line.lower().startswith("validation error:"):
            return line.split(":", 1)[1].strip()
    if lines:
        return " ".join(lines[:2]).strip()

    stdout_summary = (stdout_failure or {}).get("summary")
    if isinstance(stdout_summary, str) and stdout_summary.strip():
        return stdout_summary.strip()

    assignment_lines = normalized_section_lines(extract_markdown_section(prompt_text, "Repair Assignment"))
    for line in assignment_lines:
        lowered = line.lower()
        if "was not accepted because" in lowered:
            return line

    status_reason = activity.get("status_reason")
    if isinstance(status_reason, str) and status_reason.strip():
        return humanize_status_reason(status_reason)

    recovery_action = activity.get("recovery_action")
    if isinstance(recovery_action, str) and recovery_action.strip():
        return describe_recovery_action_failure(recovery_action)

    return "The previous attempt required repair."


def summarize_repair_request(prompt_text: str | None, activity: dict[str, Any]) -> str:
    lines = normalized_section_lines(extract_markdown_section(prompt_text, "Repair Assignment"))
    if lines:
        selected: list[str] = []
        for line in lines:
            lowered = line.lower()
            if lowered.startswith("your job in this turn is"):
                selected.append(line)
            elif lowered.startswith("preserve as much"):
                selected.append(line)
            elif lowered.startswith("do not broaden scope"):
                selected.append(line)
            if len(selected) >= 2:
                break
        if selected:
            return " ".join(selected)
        return " ".join(lines[:2])

    recovery_action = activity.get("recovery_action")
    if isinstance(recovery_action, str) and recovery_action.strip():
        return describe_recovery_action_request(recovery_action, activity)

    current_activity = activity.get("current_activity")
    if isinstance(current_activity, str) and current_activity.strip():
        return humanize_current_activity(current_activity)

    return "Repair the previous attempt while preserving valid work."


def extract_markdown_section(prompt_text: str | None, heading: str) -> str | None:
    if not isinstance(prompt_text, str) or not prompt_text.strip():
        return None

    lines = prompt_text.splitlines()
    in_section = False
    collected: list[str] = []
    target = heading.strip().lower()

    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            normalized = stripped.lstrip("#").strip().lower()
            if in_section:
                break
            if normalized == target:
                in_section = True
            continue
        if in_section:
            collected.append(raw_line)

    if not collected:
        return None
    return "\n".join(collected).strip() or None


def normalized_section_lines(section: str | None) -> list[str]:
    if not isinstance(section, str) or not section.strip():
        return []

    lines: list[str] = []
    for raw_line in section.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            break
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        lines.append(stripped)
    return lines


def humanize_symbolic_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("_", " ").strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def humanize_status_reason(value: str) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    replacements = {
        "stall_after_turn_started": "The previous attempt stalled after work had already started.",
        "stall_retry_scheduled": "The previous attempt stalled before it could finish cleanly.",
        "timeout_exhausted": "The previous attempt hit its timeout and could not finish.",
        "timeout_retry_scheduled": "The previous attempt timed out and had to be retried.",
        "missing_final_agent_message": "The previous attempt finished without producing a usable final response.",
        "missing_final_message_retry_scheduled": "The previous attempt finished without a usable final response.",
        "planning_stalled": "The previous planning attempt stalled before it could finish.",
    }
    if text in replacements:
        return replacements[text]
    return humanize_symbolic_text(text)


def describe_recovery_action_failure(recovery_action: str) -> str:
    action = recovery_action.strip()
    descriptions = {
        "recreated_workspace": "The previous attempt failed because its workspace or local environment became unusable.",
        "refreshed_workspace": "The previous attempt left the workspace in a bad state.",
        "reused_workspace": "The previous attempt had to be retried without changing scope.",
        "retry": "The previous attempt did not produce an acceptable result.",
        "planning_repair": "The previous planning response failed deterministic validation.",
        "compact_repair_retry": "The previous plan response did not satisfy the expected response contract.",
        "timeout_retry": "The previous attempt timed out before finishing.",
        "stall_retry": "The previous attempt stalled before completion.",
        "missing_final_message_retry": "The previous attempt ended without a usable final response.",
    }
    if action in descriptions:
        return descriptions[action]
    return f"The previous attempt required repair via {humanize_symbolic_text(action)}."


def describe_recovery_action_request(recovery_action: str, activity: dict[str, Any]) -> str:
    action = recovery_action.strip()
    display_name = activity.get("display_name") or activity.get("activity_id") or "this task"
    descriptions = {
        "recreated_workspace": f"Re-run {display_name} in a clean workspace and verify it completes successfully.",
        "refreshed_workspace": f"Retry {display_name} after refreshing the workspace state.",
        "reused_workspace": f"Retry {display_name} in the existing workspace without changing scope.",
        "retry": f"Retry {display_name} and return a valid result without broadening scope.",
        "planning_repair": f"Correct the previous planning response for {display_name} while preserving valid work.",
        "compact_repair_retry": f"Rewrite the plan for {display_name} so it satisfies the expected response contract.",
        "timeout_retry": f"Retry {display_name} and ensure it finishes within the allowed timeout.",
        "stall_retry": f"Retry {display_name} and make forward progress without stalling.",
        "missing_final_message_retry": f"Retry {display_name} and ensure it produces a usable final response.",
    }
    if action in descriptions:
        return descriptions[action]
    return f"Repair {display_name} and complete the same scoped work successfully."


def humanize_current_activity(current_activity: str) -> str:
    text = current_activity.strip()
    if text.startswith("Running command:"):
        return "Continue the repair attempt and verify the task completes successfully."
    return text


def resolve_variant_artifacts(
    project_root: Path,
    activity: dict[str, Any],
    summary: dict[str, Any] | None,
    *,
    variant: str,
) -> dict[str, str | None] | None:
    prompt_path = resolve_prompt_path(project_root, activity, summary)
    response_path = normalized_optional_path(summary.get("last_message_path")) if isinstance(summary, dict) else None
    stdout_path = resolve_stream_path(summary, activity, "stdout_path")
    stderr_path = resolve_stream_path(summary, activity, "stderr_path")
    structured_output_path = None
    if isinstance(summary, dict):
        report_path = summary.get("report_path")
        if isinstance(report_path, str) and report_path.strip():
            structured_output_path = report_path.strip()
    if structured_output_path is None:
        output_path = activity.get("output_path")
        if isinstance(output_path, str) and output_path.strip():
            structured_output_path = output_path.strip()

    if variant == "original":
        original_prompt_path = strip_repair_suffix(prompt_path)
        original_response_path = strip_repair_suffix(response_path)
        original_stdout_path = strip_repair_suffix(stdout_path)
        original_stderr_path = strip_repair_suffix(stderr_path)
        original_output_path = strip_repair_suffix(structured_output_path)
        derived_paths = (
            (prompt_path, original_prompt_path),
            (response_path, original_response_path),
            (stdout_path, original_stdout_path),
            (stderr_path, original_stderr_path),
            (structured_output_path, original_output_path),
        )
        if not any(
            original is not None and original != current
            for current, original in derived_paths
        ):
            return None
        prompt_path = original_prompt_path
        response_path = original_response_path
        stdout_path = original_stdout_path
        stderr_path = original_stderr_path
        structured_output_path = original_output_path
        if not any(
            path_exists(project_root, path)
            for path in (prompt_path, response_path, stdout_path, stderr_path, structured_output_path)
        ):
            return None

    return {
        "prompt_path": prompt_path,
        "response_path": response_path if path_exists(project_root, response_path) else None,
        "stdout_path": stdout_path if path_exists(project_root, stdout_path) else None,
        "stderr_path": stderr_path if path_exists(project_root, stderr_path) else None,
        "structured_output_path": structured_output_path if path_exists(project_root, structured_output_path) else None,
    }


def load_activity_summary(
    project_root: Path,
    run_id: str,
    activity: dict[str, Any],
) -> dict[str, Any] | None:
    summary_path = activity_summary_path(project_root, run_id, activity)
    if summary_path is None or not summary_path.exists():
        return None
    return load_optional_json(summary_path)


def activity_summary_path(
    project_root: Path,
    run_id: str,
    activity: dict[str, Any],
) -> Path | None:
    activity_kind = activity.get("kind")
    activity_id = activity.get("activity_id")
    if activity_kind == "task_execution" and isinstance(activity_id, str) and activity_id:
        return project_root / "runs" / run_id / "executions" / f"{activity_id}.json"
    if activity_kind in {"objective_plan", "capability_plan"}:
        output_path = activity.get("output_path")
        if not isinstance(output_path, str) or not output_path.strip():
            return None
        output_file = project_root / output_path.strip()
        if output_file.suffix != ".json":
            return None
        return output_file.with_suffix(".summary.json")
    return None


def load_artifact_text(project_root: Path, relative_path: str | None) -> str | None:
    if not isinstance(relative_path, str) or not relative_path.strip():
        return None
    artifact_path = project_root / relative_path.strip()
    if not artifact_path.exists():
        return None
    if artifact_path.suffix == ".json":
        try:
            return json.dumps(read_json(artifact_path), indent=2, sort_keys=True)
        except Exception:
            pass
    try:
        payload = read_text(artifact_path).strip()
    except OSError:
        return None
    return payload or None


def resolve_stream_path(
    summary: dict[str, Any] | None,
    activity: dict[str, Any],
    key: str,
) -> str | None:
    if isinstance(summary, dict):
        summary_path = normalized_optional_path(summary.get(key))
        if summary_path is not None:
            return summary_path
    return normalized_optional_path(activity.get(key))


def summarize_stdout_failure(project_root: Path, relative_path: str | None) -> dict[str, str] | None:
    if not isinstance(relative_path, str) or not relative_path.strip():
        return None
    stdout_path = project_root / relative_path.strip()
    if not stdout_path.exists():
        return None

    try:
        payload = read_text(stdout_path)
    except OSError:
        return None

    latest_failure: dict[str, str] | None = None
    for raw_line in payload.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        item = entry.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue

        exit_code = item.get("exit_code")
        status = str(item.get("status") or "").strip().lower()
        if not ((isinstance(exit_code, int) and exit_code != 0) or status == "failed"):
            continue

        aggregated_output = item.get("aggregated_output")
        if not isinstance(aggregated_output, str) or not aggregated_output.strip():
            continue

        command = simplify_command_text(str(item.get("command") or "").strip())
        latest_failure = {
            "summary": summarize_failure_output(aggregated_output),
            "command": command,
            "excerpt": build_failure_excerpt(aggregated_output),
            "stdout_path": relative_path.strip(),
        }

    return latest_failure


def simplify_command_text(command: str) -> str:
    if not command:
        return ""
    prefix = "/bin/zsh -lc "
    if command.startswith(prefix):
        remainder = command[len(prefix) :].strip()
        if len(remainder) >= 2 and remainder[0] == remainder[-1] and remainder[0] in {"'", '"'}:
            return remainder[1:-1]
        return remainder
    return command


def summarize_failure_output(output: str) -> str:
    lines = significant_output_lines(output)
    if not lines:
        return "The command failed without a readable stdout explanation."

    for line in lines:
        lowered = line.lower()
        if "assertionerror" in lowered:
            return line
        if "cannot find module" in lowered:
            return line
        if lowered.startswith("typeerror:") or lowered.startswith("referenceerror:") or lowered.startswith("syntaxerror:"):
            return line
        if lowered.startswith("error:"):
            return line

    for line in lines:
        lowered = line.lower()
        if line.startswith("✖ ") and "failing tests" not in lowered:
            return line[2:].strip()

    return lines[0]


def significant_output_lines(output: str) -> list[str]:
    lines: list[str] = []
    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith("ℹ "):
            continue
        if lowered.startswith("at "):
            continue
        if lowered.startswith("node.js v"):
            continue
        if lowered.startswith("require stack:"):
            continue
        if lowered.startswith("generatedmessage:"):
            continue
        if lowered.startswith("actual:") or lowered.startswith("expected:") or lowered.startswith("operator:"):
            continue
        lines.append(stripped)
    return lines


def build_failure_excerpt(output: str, *, max_lines: int = 8, max_chars: int = 900) -> str:
    lines = significant_output_lines(output)
    if not lines:
        return ""

    excerpt = "\n".join(lines[:max_lines]).strip()
    if len(excerpt) <= max_chars:
        return excerpt
    return excerpt[: max_chars - 1].rstrip() + "…"


def resolve_prompt_path(
    project_root: Path,
    activity: dict[str, Any],
    summary: dict[str, Any] | None,
) -> str | None:
    direct_prompt_path = activity.get("prompt_path")
    if isinstance(direct_prompt_path, str) and direct_prompt_path.strip():
        return direct_prompt_path.strip()

    candidate_paths: list[str] = []
    if activity.get("kind") == "task_execution":
        activity_id = activity.get("activity_id")
        run_id = activity.get("run_id")
        if isinstance(activity_id, str) and activity_id and isinstance(run_id, str) and run_id:
            candidate_paths.append(f"runs/{run_id}/prompt-logs/{activity_id}.prompt.md")

    if isinstance(summary, dict):
        for key in ("last_message_path", "stdout_path", "stderr_path", "plan_path", "report_path"):
            raw_path = summary.get(key)
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            normalized = raw_path.strip()
            candidate_paths.extend(prompt_path_candidates_for_artifact(normalized))

    output_path = activity.get("output_path")
    if isinstance(output_path, str) and output_path.strip():
        candidate_paths.extend(prompt_path_candidates_for_artifact(output_path.strip()))

    seen: set[str] = set()
    for candidate in candidate_paths:
        if candidate in seen:
            continue
        seen.add(candidate)
        if (project_root / candidate).exists():
            return candidate
    return None


def prompt_path_candidates_for_artifact(relative_path: str) -> list[str]:
    artifact_path = Path(relative_path)
    name = artifact_path.name
    candidates: list[Path] = []

    if name.endswith(".last-message.json"):
        candidates.append(artifact_path.with_name(name[: -len(".last-message.json")] + ".prompt.md"))
    if name.endswith(".stdout.jsonl"):
        candidates.append(artifact_path.with_name(name[: -len(".stdout.jsonl")] + ".prompt.md"))
    if name.endswith(".stderr.log"):
        candidates.append(artifact_path.with_name(name[: -len(".stderr.log")] + ".prompt.md"))
    if artifact_path.suffix == ".json":
        candidates.append(artifact_path.with_suffix(".prompt.md"))

    return [str(candidate) for candidate in candidates]


def normalized_optional_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def strip_repair_suffix(relative_path: str | None) -> str | None:
    if not relative_path:
        return None
    import re

    return re.sub(r"\.repair-\d+", "", relative_path)


def path_exists(project_root: Path, relative_path: str | None) -> bool:
    if not relative_path:
        return False
    return (project_root / relative_path).exists()


def infer_run_started_at(project_root: Path, run_id: str) -> str | None:
    run_dir = project_root / "runs" / run_id
    candidates = [
        run_dir / "goal.md",
        run_dir / "phase-plan.json",
        run_dir,
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        return stat_timestamp(candidate)
    return None


def stat_timestamp(path: Path) -> str:
    return (
        datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def create_app(project_root: Path):
    def route(path: str, query: dict[str, list[str]]) -> dict[str, Any]:
        if path == "/":
            return {
                "status": "ok",
                "service": "monitor-api",
                "projectRoot": str(project_root),
                "message": "This is the monitor API, not the browser frontend. Open the frontend URL printed by watch-run-web. API routes live under /api and /health.",
                "routes": {
                    "health": "/health",
                    "runs": "/api/runs",
                },
            }
        if path == "/health":
            return {
                "projectRoot": str(project_root),
                "runCount": len(list_runs_payload(project_root)["runs"]),
                "status": "ok",
            }
        if path == "/api/runs":
            return list_runs_payload(project_root)

        parts = [part for part in path.split("/") if part]
        if len(parts) < 3 or parts[0] != "api" or parts[1] != "runs":
            raise MonitorNotFoundError("Endpoint not found.")

        run_id = unquote(parts[2])
        if len(parts) == 4 and parts[3] == "dashboard":
            return build_run_dashboard_payload(project_root, run_id, events_limit=parse_limit(query, default=50))
        if len(parts) == 4 and parts[3] == "events":
            activity_id = read_optional_query_value(query, "activity_id")
            return build_events_payload(
                project_root,
                run_id,
                limit=parse_limit(query, default=50),
                activity_id=activity_id,
            )
        if len(parts) == 5 and parts[3] == "activities":
            return build_activity_detail_payload(
                project_root,
                run_id,
                unquote(parts[4]),
                events_limit=parse_limit(query, default=20),
            )
        if len(parts) == 6 and parts[3] == "activities" and parts[5] == "prompt-debug":
            return build_prompt_debug_payload(
                project_root,
                run_id,
                unquote(parts[4]),
                variant=read_optional_query_value(query, "variant"),
            )
        raise MonitorNotFoundError("Endpoint not found.")

    def route_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/api/open-file":
            raw_path = payload.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise MonitorBadRequestError("path is required.")
            return open_project_path(project_root, raw_path)
        raise MonitorNotFoundError("Endpoint not found.")

    class MonitorApiHandler(BaseHTTPRequestHandler):
        def do_OPTIONS(self) -> None:  # noqa: N802
            start_time = time.perf_counter()
            status = HTTPStatus.NO_CONTENT
            self.send_response(status)
            apply_common_headers(self)
            self.end_headers()
            log_api_request(self, status, start_time)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            start_time = time.perf_counter()
            status = HTTPStatus.OK
            try:
                payload = route(parsed.path, parse_qs(parsed.query))
                self.send_response(status)
                apply_common_headers(self)
                self.end_headers()
                self.wfile.write(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))
            except MonitorApiError as error:
                status = error.status
                self.send_response(status)
                apply_common_headers(self)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(error)}).encode("utf-8"))
            except FileNotFoundError as error:
                status = HTTPStatus.NOT_FOUND
                self.send_response(status)
                apply_common_headers(self)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(error)}).encode("utf-8"))
            except Exception as error:  # pragma: no cover - defensive path
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                self.send_response(status)
                apply_common_headers(self)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(error)}).encode("utf-8"))
            finally:
                log_api_request(self, status, start_time)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            start_time = time.perf_counter()
            status = HTTPStatus.OK
            try:
                content_length = int(self.headers.get("content-length", "0"))
                request_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                try:
                    payload = json.loads(request_body.decode("utf-8"))
                except json.JSONDecodeError as error:
                    raise MonitorBadRequestError("Request body must be valid JSON.") from error
                if not isinstance(payload, dict):
                    raise MonitorBadRequestError("Request body must be a JSON object.")
                response_payload = route_post(parsed.path, payload)
                self.send_response(status)
                apply_common_headers(self)
                self.end_headers()
                self.wfile.write(json.dumps(response_payload, indent=2, sort_keys=True).encode("utf-8"))
            except MonitorApiError as error:
                status = error.status
                self.send_response(status)
                apply_common_headers(self)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(error)}).encode("utf-8"))
            except FileNotFoundError as error:
                status = HTTPStatus.NOT_FOUND
                self.send_response(status)
                apply_common_headers(self)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(error)}).encode("utf-8"))
            except Exception as error:  # pragma: no cover - defensive path
                status = HTTPStatus.INTERNAL_SERVER_ERROR
                self.send_response(status)
                apply_common_headers(self)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(error)}).encode("utf-8"))
            finally:
                log_api_request(self, status, start_time)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    return MonitorApiHandler


def apply_common_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("access-control-allow-origin", "*")
    handler.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
    handler.send_header("access-control-allow-headers", "content-type")
    handler.send_header("cache-control", "no-store")
    handler.send_header("content-type", "application/json; charset=utf-8")


def log_api_event(event: str, **payload: Any) -> None:
    record = {"component": "monitor-api", "event": event, **payload}
    print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)


def log_api_request(handler: BaseHTTPRequestHandler, status: int | HTTPStatus, start_time: float) -> None:
    client_host, client_port = handler.client_address
    log_api_event(
        "request",
        client={"host": client_host, "port": client_port},
        duration_ms=int((time.perf_counter() - start_time) * 1000),
        method=handler.command,
        path=handler.path,
        status=int(status),
    )


def parse_limit(query: dict[str, list[str]], *, default: int) -> int:
    raw_value = read_optional_query_value(query, "limit")
    if raw_value is None:
        return default
    try:
        parsed = int(raw_value)
    except ValueError as error:
        raise MonitorBadRequestError("limit must be an integer.") from error
    if parsed <= 0:
        raise MonitorBadRequestError("limit must be greater than zero.")
    return min(parsed, 200)


def read_optional_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = values[-1].strip()
    return value or None


def resolve_openable_project_path(project_root: Path, raw_path: str) -> Path:
    candidate = raw_path.strip()
    if not candidate:
        raise MonitorBadRequestError("path is required.")

    requested_path = Path(candidate).expanduser()
    if requested_path.is_absolute():
        resolved_path = requested_path.resolve()
    else:
        resolved_path = (project_root / requested_path).resolve()

    project_root_resolved = project_root.resolve()
    try:
        resolved_path.relative_to(project_root_resolved)
    except ValueError as error:
        raise MonitorBadRequestError("Only files inside the project root can be opened.") from error

    if not resolved_path.exists():
        raise MonitorNotFoundError(f"{candidate} was not found.")

    return resolved_path


def open_local_path(target_path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(target_path)], check=True)
        return
    if os.name == "nt":  # pragma: no cover - platform-specific
        os.startfile(str(target_path))
        return
    subprocess.run(["xdg-open", str(target_path)], check=True)  # pragma: no cover - platform-specific


class MonitorApiServer:
    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread, *, project_root: Path):
        self.project_root = project_root
        self.server = server
        self.thread = thread

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_monitor_api_server(
    project_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> MonitorApiServer:
    server = ThreadingHTTPServer((host, port), create_app(project_root))
    thread = threading.Thread(target=server.serve_forever, name="monitor-api", daemon=True)
    thread.start()
    return MonitorApiServer(server, thread, project_root=project_root)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m company_orchestrator.monitor_api")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    server = ThreadingHTTPServer((args.host, args.port), create_app(project_root))
    host, port = server.server_address
    print(
        json.dumps(
            {
                "projectRoot": str(project_root),
                "status": "listening",
                "url": f"http://{host}:{port}",
            }
        ),
        flush=True,
    )
    log_api_event(
        "listening",
        host=host,
        port=port,
        project_root=str(project_root),
        url=f"http://{host}:{port}",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
