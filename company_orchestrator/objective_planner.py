from __future__ import annotations

import fnmatch
import json
import re
import shlex
import shutil
import subprocess
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from contextlib import contextmanager
from pathlib import Path
import tempfile
from typing import Any, Callable

from .executor import (
    CodexProcessStall,
    ExecutorError,
    build_codex_command,
    build_exec_environment,
    coerce_process_text,
    extract_final_response,
    extract_thread_id,
    extract_turn_failure,
    extract_usage,
    handle_codex_event_line,
    parse_jsonl_events,
    run_codex_command,
)
from .contract_authority import (
    authoritative_capability_for_contract_kind,
    capability_may_author_contract,
    contract_kind_for_descriptor,
    contract_kind_for_reference,
    is_frontend_consumption_contract_path,
)
from .filesystem import append_text, clear_text, ensure_dir, load_optional_json, read_json, write_json, write_text
from .handoffs import derive_target_tasks
from .live import (
    capability_plan_activity_id,
    ensure_activity,
    list_activities,
    note_activity_stream,
    now_timestamp,
    read_activity,
    plan_activity_id,
    record_event,
    update_activity,
)
from .observability import prompt_metrics, record_llm_call
from .objective_roots import (
    capability_owned_shared_workspace_paths,
    capability_owned_path_hints,
    capability_shared_asset_hints,
    capability_workspace_root,
    find_objective_app_root,
    find_objective_root,
)
from .output_descriptors import (
    descriptor_kind,
    descriptor_output_id,
    descriptor_path,
    descriptor_summary,
    looks_like_repo_path,
    normalize_output_descriptors,
    output_descriptor_ids,
    sanitize_output_descriptors,
)
from .parallelism import (
    canonicalize_validation_commands,
    concrete_expected_output_paths,
    effective_sandbox_mode,
    infer_execution_metadata,
    normalize_task_artifact_descriptors,
    path_pattern_conflict,
)
from .prompts import (
    PHASE_SEQUENCE,
    approved_scope_overrides_for_objective,
    build_capability_planning_prompt_packet,
    build_capability_prompt_payload,
    build_objective_planning_prompt_packet,
    build_planning_prompt_payload,
    objective_summary_text,
    build_validation_environment_hints,
    build_release_repair_inputs,
    compile_task_context_packet,
    collect_prior_phase_artifacts,
    collect_prior_phase_reports,
    collect_related_app_prior_phase_reports,
    infer_report_capability,
    lookup_dotted_path,
    preview_resolved_inputs,
    release_repair_input_refs,
    render_capability_planning_prompt,
    render_objective_planning_prompt,
    scope_override_phrases,
    scope_override_tokens,
)
from .recovery import prepare_activity_retry, reconcile_for_command
from .schemas import SchemaValidationError, validate_document
from .task_graph import write_objective_task_graph_manifest
from .task_graph import update_run_file_graph_capability_plan
from .timeout_policy import resolve_planning_timeout_policy, timeout_final_message, timeout_retry_message
from .worktree_manager import cleanup_phase_task_worktrees, integration_workspace_path, normalize_repo_relative_path


class PlanningLimiter:
    def __init__(self, max_concurrency: int) -> None:
        self.max_concurrency = max(1, max_concurrency)
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._waiting = 0

    def acquire(self) -> tuple[int | None, int]:
        started = time.monotonic()
        if self._semaphore.acquire(blocking=False):
            return None, 0
        with self._lock:
            self._waiting += 1
            queue_position = self._waiting
        self._semaphore.acquire()
        with self._lock:
            self._waiting = max(0, self._waiting - 1)
        return queue_position, int((time.monotonic() - started) * 1000)

    def release(self) -> None:
        self._semaphore.release()


MAX_MANAGER_PLANNING_REPAIR_ATTEMPTS = 1
MAX_MANAGER_PLANNING_MISSING_FINAL_MESSAGE_RETRIES = 1
PLANNING_STALL_TIMEOUT_MIN_SECONDS = 60
PLANNING_STALL_TIMEOUT_MAX_SECONDS = 180
MANAGER_FIXABLE_PLANNING_ERROR_TYPES = (ExecutorError, SchemaValidationError, ValueError)


def planning_stall_timeout_seconds(timeout_seconds: int) -> int:
    return max(
        1,
        min(
            timeout_seconds,
            max(
                PLANNING_STALL_TIMEOUT_MIN_SECONDS,
                min(PLANNING_STALL_TIMEOUT_MAX_SECONDS, max(1, timeout_seconds // 4)),
            ),
        ),
    )


def planning_attempt_stream_name(output_prefix: str, attempt: int, suffix: str) -> str:
    if attempt <= 1:
        return f"{output_prefix}.{suffix}"
    return f"{output_prefix}.attempt-{attempt}.{suffix}"


def validate_scope_override_coverage_for_objective_outline(
    project_root: Path,
    run_id: str,
    phase: str,
    objective_id: str,
    outline: dict[str, Any],
) -> None:
    overrides = approved_scope_overrides_for_objective(
        project_root,
        run_id,
        objective_id,
        phase=phase,
    )
    if not overrides:
        return
    text = " ".join(
        [
            str(outline.get("summary") or ""),
            " ".join(str(item) for item in outline.get("dependency_notes", []) if item),
            " ".join(str(lane.get("objective") or "") for lane in outline.get("capability_lanes", [])),
            " ".join(str(edge.get("reason") or "") for edge in outline.get("collaboration_edges", [])),
            " ".join(
                str(deliverable.get("description") or deliverable.get("output_id") or deliverable.get("path") or "")
                for edge in outline.get("collaboration_edges", [])
                for deliverable in edge.get("deliverables", [])
                if isinstance(deliverable, dict)
            ),
        ]
    )
    missing = [item["summary"] for item in overrides if not _plan_text_covers_scope_override(text, item["summary"])]
    if missing:
        raise ValueError(
            "Objective outline does not materially incorporate approved scope overrides: " + "; ".join(missing)
        )


def validate_scope_override_coverage_for_capability_plan(
    project_root: Path,
    run_id: str,
    phase: str,
    objective_id: str,
    capability_plan: dict[str, Any],
) -> None:
    overrides = approved_scope_overrides_for_objective(
        project_root,
        run_id,
        objective_id,
        phase=phase,
    )
    if not overrides:
        return
    text = " ".join(
        [
            str(capability_plan.get("summary") or ""),
            " ".join(
                str(task.get("objective") or "")
                + " "
                + " ".join(str(item) for item in task.get("done_when", []) if item)
                + " "
                + " ".join(
                    str(output.get("description") or output.get("output_id") or output.get("path") or "")
                    for output in task.get("expected_outputs", [])
                    if isinstance(output, dict)
                )
                for task in capability_plan.get("tasks", [])
                if isinstance(task, dict)
            ),
        ]
    )
    missing = [item["summary"] for item in overrides if not _plan_text_covers_scope_override(text, item["summary"])]
    if missing:
        raise ValueError(
            "Capability plan does not materially incorporate approved scope overrides: " + "; ".join(missing)
        )


def _plan_text_covers_scope_override(plan_text: str, summary: str) -> bool:
    searchable = plan_text.lower()
    phrases = [phrase for phrase in scope_override_phrases(summary) if phrase]
    if any(phrase in searchable for phrase in phrases):
        return True
    summary_tokens = set(scope_override_tokens(summary))
    plan_tokens = set(scope_override_tokens(plan_text))
    return len(summary_tokens & plan_tokens) >= 2


def _normalize_and_validate_objective_outline(
    project_root: Path,
    run_id: str,
    phase: str,
    objective_id: str,
    objective: dict[str, Any],
    payload: Any,
) -> tuple[dict[str, Any], dict[str, str]]:
    outline, identity_adjustments = normalize_objective_outline(
        project_root,
        payload,
        run_id=run_id,
        phase=phase,
        objective=objective,
    )
    validate_scope_override_coverage_for_objective_outline(
        project_root,
        run_id,
        phase,
        objective_id,
        outline,
    )
    validate_objective_outline_planning_input_addressability(
        project_root,
        run_id,
        phase,
        objective_id,
        outline,
    )
    return outline, identity_adjustments


def _normalize_and_validate_capability_plan(
    project_root: Path,
    run_id: str,
    phase: str,
    objective_id: str,
    capability: str,
    objective_outline: dict[str, Any],
    default_sandbox_mode: str,
    payload: Any,
) -> tuple[dict[str, Any], dict[str, str]]:
    plan, identity_adjustments = normalize_capability_plan(
        project_root,
        payload,
        run_id=run_id,
        phase=phase,
        objective_id=objective_id,
        capability=capability,
        objective_outline=objective_outline,
        default_sandbox_mode=default_sandbox_mode,
    )
    validate_scope_override_coverage_for_capability_plan(
        project_root,
        run_id,
        phase,
        objective_id,
        plan,
    )
    validate_capability_plan_planning_input_addressability(
        project_root,
        run_id,
        objective_id,
        capability,
        objective_outline,
        plan,
    )
    return plan, identity_adjustments


def validate_prompt_packet_input_refs(
    input_refs: list[Any],
    planning_packet: dict[str, Any],
    *,
    owner_label: str,
) -> None:
    invalid_refs: list[str] = []
    for value in input_refs:
        if not isinstance(value, str):
            continue
        input_ref = value.strip()
        if not input_ref.startswith("Planning Inputs."):
            continue
        resolved = lookup_dotted_path(planning_packet, input_ref.removeprefix("Planning Inputs."))
        if isinstance(resolved, dict) and isinstance(resolved.get("missing_path"), str):
            invalid_refs.append(input_ref)
            continue
        if resolved is None:
            invalid_refs.append(input_ref)
    if invalid_refs:
        joined = ", ".join(sorted(dedupe_strings(invalid_refs)))
        raise ExecutorError(f"{owner_label} referenced unavailable planning inputs: {joined}")


def validate_objective_outline_planning_input_addressability(
    project_root: Path,
    run_id: str,
    phase: str,
    objective_id: str,
    outline: dict[str, Any],
) -> None:
    planning_payload = build_planning_prompt_payload(project_root, run_id, objective_id)
    planning_packet = build_objective_planning_prompt_packet(planning_payload)
    for lane in outline.get("capability_lanes", []):
        capability = str(lane.get("capability") or "").strip() or "unknown"
        validate_prompt_packet_input_refs(
            list(lane.get("inputs", [])),
            planning_packet,
            owner_label=f"Objective outline capability lane {capability}",
        )


def validate_capability_plan_planning_input_addressability(
    project_root: Path,
    run_id: str,
    objective_id: str,
    capability: str,
    objective_outline: dict[str, Any],
    capability_plan: dict[str, Any],
) -> None:
    planning_payload = build_capability_prompt_payload(
        project_root,
        run_id,
        objective_id,
        capability,
        objective_outline,
    )
    planning_packet = build_capability_planning_prompt_packet(planning_payload)
    for task in capability_plan.get("tasks", []):
        task_id = str(task.get("task_id") or "").strip() or "unknown-task"
        validate_prompt_packet_input_refs(
            list(task.get("inputs", [])),
            planning_packet,
            owner_label=f"Capability plan task {task_id}",
        )


@contextmanager
def quarantined_objective_phase_artifacts(
    project_root: Path,
    run_id: str,
    phase: str,
    objective_id: str,
    *,
    enabled: bool,
):
    archived: dict[str, Any] = {"archived_task_ids": [], "archive_path": None}
    if not enabled:
        yield archived
        return
    run_dir = project_root / "runs" / run_id
    backup_root = Path(tempfile.mkdtemp(prefix=f"orchestrator-replace-{run_id}-{objective_id}-"))
    archived_task_ids = sorted(
        set(objective_phase_task_ids(run_dir, phase, objective_id))
        | set(objective_phase_task_activity_ids(project_root, run_id, phase, objective_id))
    )
    archived["archived_task_ids"] = archived_task_ids
    moved_paths: list[tuple[Path, Path]] = []
    try:
        for source in iter_objective_phase_artifacts(project_root, run_dir, phase, objective_id):
            relative = source.relative_to(run_dir)
            destination = backup_root / relative
            ensure_dir(destination.parent)
            shutil.move(str(source), str(destination))
            moved_paths.append((source, destination))
        yield archived
    except BaseException:
        restore_quarantined_paths(moved_paths)
        shutil.rmtree(backup_root, ignore_errors=True)
        raise
    else:
        archive_dir = ensure_dir(run_dir / "archive" / "objective-replans" / phase / objective_id)
        archive_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", now_timestamp())
        archive_path = archive_dir / archive_name
        counter = 1
        while archive_path.exists():
            counter += 1
            archive_path = archive_dir / f"{archive_name}-{counter}"
        shutil.move(str(backup_root), str(archive_path))
        archived["archive_path"] = str(archive_path.relative_to(run_dir))
        try:
            cleanup_phase_task_worktrees(project_root, run_id, archived_task_ids)
        except Exception:
            pass


def iter_objective_phase_artifacts(project_root: Path, run_dir: Path, phase: str, objective_id: str) -> list[Path]:
    candidates: list[Path] = []
    task_ids = set(objective_phase_task_ids(run_dir, phase, objective_id))
    task_ids.update(objective_phase_task_activity_ids(project_root, run_dir.name, phase, objective_id))
    tasks_dir = run_dir / "tasks"
    if tasks_dir.exists():
        for path in sorted(tasks_dir.glob("*.json")):
            payload = read_json(path)
            if payload.get("phase") == phase and payload.get("objective_id") == objective_id:
                candidates.append(path)
    handoffs_dir = run_dir / "collaboration-plans"
    if handoffs_dir.exists():
        for path in sorted(handoffs_dir.glob("*.json")):
            payload = read_json(path)
            if payload.get("phase") == phase and payload.get("objective_id") == objective_id:
                candidates.append(path)
    manager_plans_dir = run_dir / "manager-plans"
    if manager_plans_dir.exists():
        candidates.extend(sorted(manager_plans_dir.glob(f"{phase}-{objective_id}*")))
    reports_dir = run_dir / "reports"
    if reports_dir.exists():
        for task_id in sorted(task_ids):
            path = reports_dir / f"{task_id}.json"
            if path.exists():
                candidates.append(path)
    executions_dir = run_dir / "executions"
    if executions_dir.exists():
        execution_suffixes = [".json", ".stdout.jsonl", ".stderr.log", ".last-message.json"]
        for task_id in sorted(task_ids):
            for suffix in execution_suffixes:
                path = executions_dir / f"{task_id}{suffix}"
                if path.exists():
                    candidates.append(path)
    prompt_logs_dir = run_dir / "prompt-logs"
    if prompt_logs_dir.exists():
        prompt_suffixes = [".prompt.md", ".prompt.json"]
        for task_id in sorted(task_ids):
            for suffix in prompt_suffixes:
                path = prompt_logs_dir / f"{task_id}{suffix}"
                if path.exists():
                    candidates.append(path)
    live_activities_dir = run_dir / "live" / "activities"
    if live_activities_dir.exists():
        for task_id in sorted(objective_phase_task_activity_ids(project_root, run_dir.name, phase, objective_id)):
            path = live_activities_dir / f"{task_id}.json"
            if path.exists():
                candidates.append(path)
    bundles_dir = run_dir / "bundles"
    if bundles_dir.exists():
        for path in sorted(bundles_dir.glob("*.json")):
            payload = read_json(path)
            if payload.get("phase") == phase and payload.get("objective_id") == objective_id:
                candidates.append(path)
    unique_paths: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.exists():
            continue
        seen.add(path)
        unique_paths.append(path)
    return unique_paths


def objective_phase_task_ids(run_dir: Path, phase: str, objective_id: str) -> list[str]:
    tasks_dir = run_dir / "tasks"
    if not tasks_dir.exists():
        return []
    task_ids: list[str] = []
    for path in sorted(tasks_dir.glob("*.json")):
        payload = read_json(path)
        if payload.get("phase") == phase and payload.get("objective_id") == objective_id:
            task_ids.append(payload["task_id"])
    return task_ids


def objective_phase_task_activity_ids(project_root: Path, run_id: str, phase: str, objective_id: str) -> list[str]:
    task_ids: list[str] = []
    for activity in list_activities(project_root, run_id, phase=phase):
        if activity.get("kind") != "task_execution":
            continue
        if activity.get("objective_id") != objective_id:
            continue
        activity_id = activity.get("activity_id")
        if isinstance(activity_id, str):
            task_ids.append(activity_id)
    return task_ids


def write_phase_plan_summary(
    project_root: Path,
    run_id: str,
    phase: str,
    *,
    max_concurrency: int | None = None,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    objective_map = read_json(run_dir / "objective-map.json")
    summary_path = run_dir / "manager-plans" / f"{phase}-phase-plan-summary.json"
    existing = load_optional_json(summary_path) or {}
    planned_objectives: list[dict[str, Any]] = []
    for objective in objective_map.get("objectives", []):
        objective_summary_path = run_dir / "manager-plans" / f"{phase}-{objective['objective_id']}.summary.json"
        if objective_summary_path.exists():
            planned_objectives.append(read_json(objective_summary_path))
    payload = {
        "run_id": run_id,
        "phase": phase,
        "planned_objectives": planned_objectives,
        "max_concurrency": (
            max_concurrency
            if max_concurrency is not None
            else existing.get("max_concurrency", 1)
        ),
    }
    write_json(summary_path, payload)
    return payload


def restore_quarantined_paths(moved_paths: list[tuple[Path, Path]]) -> None:
    for original, backup in reversed(moved_paths):
        ensure_dir(original.parent)
        if backup.exists():
            shutil.move(str(backup), str(original))


def plan_objective(
    project_root: Path,
    run_id: str,
    objective_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    replace: bool = False,
    timeout_seconds: int | None = None,
    max_concurrency: int = 3,
    allow_recovery_blocked: bool = False,
    skip_reconcile: bool = False,
    planning_limiter: PlanningLimiter | None = None,
    refresh_phase_summary: bool = True,
    repair_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not skip_reconcile:
        reconcile_for_command(project_root, run_id, apply=True, allow_blocked=allow_recovery_blocked)
    run_dir = project_root / "runs" / run_id
    phase = read_json(run_dir / "phase-plan.json")["current_phase"]
    plans_dir = ensure_dir(run_dir / "manager-plans")
    objective = find_objective(run_dir, objective_id)
    planning_limiter = planning_limiter or PlanningLimiter(max_concurrency)
    objective_result: dict[str, Any] = {
        "events": [],
        "stdout_path": None,
        "stderr_path": None,
        "last_message_path": None,
        "identity_adjustments": {},
        "attempt": 1,
        "recovery_action": None,
    }
    outline_path = plans_dir / f"{phase}-{objective_id}.outline.json"
    has_scope_overrides = bool(
        approved_scope_overrides_for_objective(
            project_root,
            run_id,
            objective_id,
            phase=phase,
        )
    )
    reuse_outline_for_repair = should_reuse_existing_outline_for_repair(
        replace=replace,
        objective=objective,
        repair_context=repair_context,
    )
    preloaded_outline = None
    if reuse_outline_for_repair:
        existing_outline = load_valid_document(project_root, outline_path, "objective-outline.v1")
        if existing_outline is not None:
            preloaded_outline = sanitize_outline_for_release_repair(
                project_root,
                run_id,
                objective_id=objective_id,
                outline=existing_outline,
                repair_context=repair_context,
            )
    with quarantined_objective_phase_artifacts(project_root, run_id, phase, objective_id, enabled=replace) as replace_artifacts:
        if phase == "polish":
            outline = build_deterministic_polish_outline(
                project_root,
                run_id=run_id,
                objective=objective,
            )
            write_json(outline_path, outline)
            plan, capability_summaries = build_deterministic_polish_objective_plan(
                project_root,
                run_id,
                objective=objective,
                outline=outline,
                sandbox_mode=sandbox_mode,
            )
            validate_objective_plan_contents(project_root, plan, objective)
            validate_planned_task_inputs(project_root, run_id, phase, objective_id, plan["tasks"])
            materialize_objective_plan(project_root, run_id, plan, replace=replace)
            planning_mode = "deterministic_polish"
            objective_result["recovery_action"] = "deterministic_polish_plan"
        else:
            outline = preloaded_outline if preloaded_outline is not None else (
                None if replace and not reuse_outline_for_repair else load_valid_document(project_root, outline_path, "objective-outline.v1")
            )
            if outline is not None:
                try:
                    validate_scope_override_coverage_for_objective_outline(
                        project_root,
                        run_id,
                        phase,
                        objective_id,
                        outline,
                    )
                except ValueError:
                    outline = None
            fast_path_used = False
            if objective_uses_single_capability_fast_path(phase, objective) and not has_scope_overrides and (replace or outline is None):
                outline = build_single_capability_fast_path_outline(
                    project_root,
                    run_dir,
                    run_id=run_id,
                    phase=phase,
                    objective=objective,
                )
                write_json(outline_path, outline)
                fast_path_used = True
                record_event(
                    project_root,
                    run_id,
                    phase=phase,
                    activity_id=plan_activity_id(phase, objective_id),
                    event_type="planning.fast_path",
                    message=f"Used single-capability fast path for objective {objective_id}.",
                    payload={"capabilities": list(objective.get("capabilities", []))},
                )
            elif outline is None:
                objective_prompt = render_objective_planning_prompt(
                    project_root,
                    run_id,
                    objective_id,
                    ignore_existing_phase_tasks=replace,
                    repair_context=repair_context,
                )
                objective_execution_prompt = build_planning_prompt(
                    (project_root / objective_prompt["prompt_path"]).read_text(encoding="utf-8")
                )
                try:
                    objective_result = execute_planning_activity(
                        project_root,
                        run_id,
                        phase=phase,
                        activity_id=plan_activity_id(phase, objective_id),
                        kind="objective_plan",
                        entity_id=objective_id,
                        display_name=f"Plan {objective_id}",
                        assigned_role=f"objectives.{objective_id}.objective-manager",
                        prompt_metadata=objective_prompt,
                        execution_prompt=objective_execution_prompt,
                        output_schema_name="objective-outline.response.v1",
                        output_prefix=f"{phase}-{objective_id}",
                        failure_label=f"objective {objective_id}",
                        sandbox_mode=sandbox_mode,
                        codex_path=codex_path,
                        timeout_seconds=timeout_seconds,
                        planning_limiter=planning_limiter,
                    )
                    try:
                        outline, identity_adjustments = normalize_objective_outline(
                            project_root,
                            objective_result["payload"],
                            run_id=run_id,
                            phase=phase,
                            objective=objective,
                        )
                        validate_scope_override_coverage_for_objective_outline(
                            project_root,
                            run_id,
                            phase,
                            objective_id,
                            outline,
                        )
                    except MANAGER_FIXABLE_PLANNING_ERROR_TYPES as exc:
                        objective_result, outline, identity_adjustments = repair_invalid_planning_response(
                            project_root,
                            run_id,
                            phase=phase,
                            activity_id=plan_activity_id(phase, objective_id),
                            kind="objective_plan",
                            entity_id=objective_id,
                            display_name=f"Plan {objective_id}",
                            assigned_role=f"objectives.{objective_id}.objective-manager",
                            base_prompt_metadata=objective_prompt,
                            base_execution_prompt=objective_execution_prompt,
                            output_schema_name="objective-outline.response.v1",
                            display_schema_name="objective-outline.v1",
                            output_prefix=f"{phase}-{objective_id}",
                            failure_label=f"objective {objective_id}",
                            sandbox_mode=sandbox_mode,
                            codex_path=codex_path,
                            timeout_seconds=timeout_seconds,
                            planning_limiter=planning_limiter,
                            previous_payload=objective_result["payload"],
                            validation_error=str(exc),
                            normalize_payload=lambda repair_payload: _normalize_and_validate_objective_outline(
                                project_root,
                                run_id,
                                phase,
                                objective_id,
                                objective,
                                repair_payload,
                            ),
                            repair_context=repair_context,
                        )
                except BaseException as exc:
                    mark_objective_planning_failed(
                        project_root,
                        run_id,
                        phase=phase,
                        objective_id=objective_id,
                        assigned_role=f"objectives.{objective_id}.objective-manager",
                        message=str(exc),
                        reason="objective_planning_failed",
                    )
                    raise
                objective_result["identity_adjustments"].update(identity_adjustments)
                write_json(outline_path, outline)
            else:
                objective_result["recovery_action"] = (
                    "reused_valid_outline_for_release_repair" if reuse_outline_for_repair else "reused_valid_outline"
                )
                if reuse_outline_for_repair:
                    write_json(outline_path, outline)
                    record_event(
                        project_root,
                        run_id,
                        phase=phase,
                        activity_id=plan_activity_id(phase, objective_id),
                        event_type="planning.reuse_outline",
                        message=f"Reused existing outline for objective {objective_id} during polish release repair.",
                        payload={"objective_id": objective_id},
                    )
            try:
                capability_summaries, capability_plans = plan_capabilities_for_objective(
                    project_root,
                    run_id,
                    objective_id,
                    outline["capability_lanes"],
                    objective_outline=outline,
                    replace=replace,
                    sandbox_mode=sandbox_mode,
                    codex_path=codex_path,
                    timeout_seconds=timeout_seconds,
                    max_concurrency=max_concurrency,
                    planning_limiter=planning_limiter,
                    repair_context=repair_context,
                )
                post_validation_capability_repairs: set[str] = set()
                objective_post_validation_repaired = False
                while True:
                    try:
                        plan = aggregate_capability_plans(
                            project_root,
                            run_id,
                            phase,
                            objective_id,
                            outline,
                            capability_plans,
                        )
                        planning_mode = "capability_managed"
                        if fast_path_used:
                            planning_mode = "single_capability_fast_path"

                        validate_objective_plan_contents(project_root, plan, objective)
                        validate_planned_task_inputs(project_root, run_id, plan["phase"], plan["objective_id"], plan["tasks"])
                        materialize_objective_plan(project_root, run_id, plan, replace=replace)
                        break
                    except MANAGER_FIXABLE_PLANNING_ERROR_TYPES as exc:
                        task_id = extract_task_id_from_planning_error(str(exc))
                        capability = capability_for_task_id(capability_plans, task_id) if task_id else None
                        if capability and capability not in post_validation_capability_repairs:
                            repaired_summary, repaired_plan = repair_capability_plan_after_validation_error(
                                project_root,
                                run_id,
                                phase=phase,
                                objective_id=objective_id,
                                capability=capability,
                                objective_outline=outline,
                                objective=objective,
                                capability_plans=capability_plans,
                                validation_error=str(exc),
                                sandbox_mode=sandbox_mode,
                                codex_path=codex_path,
                                timeout_seconds=timeout_seconds,
                                planning_limiter=planning_limiter,
                            )
                            capability_summaries = [
                                repaired_summary if summary.get("capability") == capability else summary
                                for summary in capability_summaries
                            ]
                            capability_plans = [
                                repaired_plan if str(plan_item.get("capability")) == capability else plan_item
                                for plan_item in capability_plans
                            ]
                            post_validation_capability_repairs.add(capability)
                            if not objective_result.get("recovery_action"):
                                objective_result["recovery_action"] = "planning_repair"
                            continue
                        if not objective_post_validation_repaired:
                            objective_result, outline, identity_adjustments = repair_objective_outline_after_validation_error(
                                project_root,
                                run_id,
                                phase=phase,
                                objective_id=objective_id,
                                objective=objective,
                                validation_error=str(exc),
                                sandbox_mode=sandbox_mode,
                                codex_path=codex_path,
                                timeout_seconds=timeout_seconds,
                                planning_limiter=planning_limiter,
                            )
                            objective_result["identity_adjustments"].update(identity_adjustments)
                            write_json(outline_path, outline)
                            capability_summaries, capability_plans = plan_capabilities_for_objective(
                                project_root,
                                run_id,
                                objective_id,
                                outline["capability_lanes"],
                                objective_outline=outline,
                                replace=True,
                                sandbox_mode=sandbox_mode,
                                codex_path=codex_path,
                                timeout_seconds=timeout_seconds,
                                max_concurrency=max_concurrency,
                                planning_limiter=planning_limiter,
                                repair_context=build_post_validation_repair_context("post-aggregation-objective-validation", str(exc)),
                            )
                            objective_post_validation_repaired = True
                            post_validation_capability_repairs.clear()
                            continue
                        raise
            except BaseException as exc:
                mark_objective_planning_failed(
                    project_root,
                    run_id,
                    phase=phase,
                    objective_id=objective_id,
                    assigned_role=f"objectives.{objective_id}.objective-manager",
                    message=str(exc),
                    reason="capability_planning_failed" if outline is not None else "objective_planning_failed",
                )
                raise

    summary_recovery_action = objective_result["recovery_action"]
    if not summary_recovery_action:
        for capability_summary in capability_summaries:
            capability_recovery_action = capability_summary.get("recovery_action")
            if isinstance(capability_recovery_action, str) and capability_recovery_action:
                summary_recovery_action = capability_recovery_action
                break
    summary = {
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "thread_id": extract_thread_id(objective_result["events"]),
        "usage": extract_usage(objective_result["events"]),
        "plan_path": f"runs/{run_id}/manager-plans/{phase}-{objective_id}.json",
        "task_ids": [task["task_id"] for task in plan["tasks"]],
        "bundle_ids": [bundle["bundle_id"] for bundle in plan["bundle_plan"]],
        "handoff_ids": [handoff["handoff_id"] for handoff in plan.get("collaboration_handoffs", [])],
        "stdout_path": objective_result["stdout_path"],
        "stderr_path": objective_result["stderr_path"],
        "last_message_path": objective_result["last_message_path"],
        "identity_adjustments": objective_result["identity_adjustments"],
        "planning_mode": planning_mode,
        "capability_summaries": capability_summaries,
        "attempt": objective_result["attempt"],
        "recovery_action": summary_recovery_action,
        "max_concurrency": max_concurrency,
    }
    if replace_artifacts.get("archive_path") is not None:
        summary["replaced_archive_path"] = replace_artifacts["archive_path"]
        summary["replaced_task_ids"] = list(replace_artifacts.get("archived_task_ids", []))
    write_json(plans_dir / f"{phase}-{objective_id}.summary.json", summary)
    if refresh_phase_summary:
        write_phase_plan_summary(project_root, run_id, phase)
    activity_status = "recovered" if objective_result["attempt"] > 1 or summary_recovery_action else "completed"
    ensure_activity(
        project_root,
        run_id,
        activity_id=plan_activity_id(phase, objective_id),
        kind="objective_plan",
        entity_id=objective_id,
        phase=phase,
        objective_id=objective_id,
        display_name=f"Plan {objective_id}",
        assigned_role=f"objectives.{objective_id}.objective-manager",
        status=activity_status,
        progress_stage=activity_status,
        current_activity=plan["summary"],
        stdout_path=objective_result["stdout_path"],
        stderr_path=objective_result["stderr_path"],
        output_path=f"runs/{run_id}/manager-plans/{phase}-{objective_id}.json",
        process_metadata=None,
        recovered_at=now_timestamp() if activity_status == "recovered" else None,
        recovery_action=summary_recovery_action,
    )
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=plan_activity_id(phase, objective_id),
        event_type="planning.completed",
        message=f"Planning activity for objective {objective_id} completed.",
        payload={
            "plan_path": f"runs/{run_id}/manager-plans/{phase}-{objective_id}.json",
            "attempt": objective_result["attempt"],
            "recovery_action": summary_recovery_action,
        },
    )
    return summary


def mark_objective_planning_failed(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    objective_id: str,
    assigned_role: str,
    message: str,
    reason: str,
) -> None:
    activity_id = plan_activity_id(phase, objective_id)
    ensure_activity(
        project_root,
        run_id,
        activity_id=activity_id,
        kind="objective_plan",
        entity_id=objective_id,
        phase=phase,
        objective_id=objective_id,
        display_name=f"Plan {objective_id}",
        assigned_role=assigned_role,
        status="failed",
        progress_stage="failed",
        current_activity=message,
        process_metadata=None,
        status_reason=reason,
    )
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=activity_id,
        event_type="planning.failed",
        message=f"Planning activity for objective {objective_id} failed.",
        payload={"error": message, "reason": reason},
    )


def plan_phase(
    project_root: Path,
    run_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    replace: bool = False,
    timeout_seconds: int | None = None,
    max_concurrency: int = 3,
) -> dict[str, Any]:
    reconcile_for_command(project_root, run_id, apply=True)
    run_dir = project_root / "runs" / run_id
    phase = read_json(run_dir / "phase-plan.json")["current_phase"]
    objective_map = read_json(run_dir / "objective-map.json")
    objective_ids = [objective["objective_id"] for objective in objective_map["objectives"]]
    objective_parallelism = max(1, min(max_concurrency, len(objective_ids))) if objective_ids else 1
    initialize_phase_objective_queue(
        project_root,
        run_id,
        phase=phase,
        objective_ids=objective_ids,
    )
    summaries_by_objective: dict[str, dict[str, Any]] = {}
    running: dict[Any, str] = {}
    pending_objective_ids = list(objective_ids)
    first_error: BaseException | None = None

    with ThreadPoolExecutor(max_workers=objective_parallelism) as pool:
        while pending_objective_ids and len(running) < objective_parallelism and first_error is None:
            objective_id = pending_objective_ids.pop(0)
            refresh_phase_objective_queue_positions(
                project_root,
                run_id,
                phase=phase,
                queued_objective_ids=pending_objective_ids,
            )
            future = submit_phase_objective_planning(
                pool,
                project_root,
                run_id,
                objective_id,
                phase=phase,
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                replace=replace,
                timeout_seconds=timeout_seconds,
            )
            running[future] = objective_id
        refresh_phase_objective_queue_positions(
            project_root,
            run_id,
            phase=phase,
            queued_objective_ids=pending_objective_ids,
        )

        while running:
            done, _ = wait(tuple(running.keys()), return_when=FIRST_COMPLETED)
            for future in done:
                objective_id = running.pop(future)
                try:
                    summaries_by_objective[objective_id] = future.result()
                except BaseException as exc:  # pragma: no cover - exercised in failure tests via raise below
                    if first_error is None:
                        first_error = exc
            while pending_objective_ids and len(running) < objective_parallelism and first_error is None:
                objective_id = pending_objective_ids.pop(0)
                refresh_phase_objective_queue_positions(
                    project_root,
                    run_id,
                    phase=phase,
                    queued_objective_ids=pending_objective_ids,
                )
                future = submit_phase_objective_planning(
                    pool,
                    project_root,
                    run_id,
                    objective_id,
                    phase=phase,
                    sandbox_mode=sandbox_mode,
                    codex_path=codex_path,
                    replace=replace,
                    timeout_seconds=timeout_seconds,
                )
                running[future] = objective_id
            refresh_phase_objective_queue_positions(
                project_root,
                run_id,
                phase=phase,
                queued_objective_ids=pending_objective_ids,
            )

    if first_error is not None:
        mark_phase_objectives_abandoned(
            project_root,
            run_id,
            phase=phase,
            objective_ids=pending_objective_ids,
            reason="phase_planning_aborted",
            message="Objective planning was not started because an earlier objective planning failure aborted the phase scheduler.",
        )
        raise first_error

    payload = write_phase_plan_summary(project_root, run_id, phase, max_concurrency=max_concurrency)
    return payload


def initialize_phase_objective_queue(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    objective_ids: list[str],
) -> None:
    for position, objective_id in enumerate(objective_ids, start=1):
        ensure_activity(
            project_root,
            run_id,
            activity_id=plan_activity_id(phase, objective_id),
            kind="objective_plan",
            entity_id=objective_id,
            phase=phase,
            objective_id=objective_id,
            display_name=f"Plan {objective_id}",
            assigned_role=f"objectives.{objective_id}.objective-manager",
            status="queued",
            progress_stage="queued",
            queue_position=position,
            current_activity="Waiting for objective planning slot.",
        )


def refresh_phase_objective_queue_positions(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    queued_objective_ids: list[str],
) -> None:
    for position, objective_id in enumerate(queued_objective_ids, start=1):
        update_activity(
            project_root,
            run_id,
            plan_activity_id(phase, objective_id),
            status="queued",
            progress_stage="queued",
            queue_position=position,
            current_activity="Waiting for objective planning slot.",
            status_reason=None,
        )


def submit_phase_objective_planning(
    pool: ThreadPoolExecutor,
    project_root: Path,
    run_id: str,
    objective_id: str,
    *,
    phase: str,
    sandbox_mode: str,
    codex_path: str,
    replace: bool,
    timeout_seconds: int | None,
):
    update_activity(
        project_root,
        run_id,
        plan_activity_id(phase, objective_id),
        status="running",
        progress_stage="running",
        queue_position=None,
        current_activity="Objective planning workflow started.",
        status_reason=None,
    )
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=plan_activity_id(phase, objective_id),
        event_type="planning.slot_acquired",
        message=f"Objective {objective_id} acquired a phase planning slot.",
        payload={"objective_id": objective_id},
    )
    return pool.submit(
        plan_objective,
        project_root,
        run_id,
        objective_id,
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        replace=replace,
        timeout_seconds=timeout_seconds,
        max_concurrency=1,
        skip_reconcile=True,
        planning_limiter=PlanningLimiter(1),
        refresh_phase_summary=False,
    )


def mark_phase_objectives_abandoned(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    objective_ids: list[str],
    reason: str,
    message: str,
) -> None:
    for objective_id in objective_ids:
        ensure_activity(
            project_root,
            run_id,
            activity_id=plan_activity_id(phase, objective_id),
            kind="objective_plan",
            entity_id=objective_id,
            phase=phase,
            objective_id=objective_id,
            display_name=f"Plan {objective_id}",
            assigned_role=f"objectives.{objective_id}.objective-manager",
            status="abandoned",
            progress_stage="abandoned",
            queue_position=None,
            current_activity=message,
            status_reason=reason,
        )


def plan_capabilities_for_objective(
    project_root: Path,
    run_id: str,
    objective_id: str,
    lanes: list[dict[str, Any]],
    *,
    objective_outline: dict[str, Any],
    replace: bool,
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    max_concurrency: int,
    planning_limiter: PlanningLimiter,
    repair_context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if max_concurrency <= 1 or len(lanes) <= 1:
        capability_summaries = []
        capability_plans = []
        for lane in lanes:
            capability_summary, capability_plan = plan_capability(
                project_root,
                run_id,
                objective_id,
                lane["capability"],
                objective_outline=objective_outline,
                replace=replace,
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                timeout_seconds=timeout_seconds,
                planning_limiter=planning_limiter,
                repair_context=repair_context,
            )
            capability_summaries.append(capability_summary)
            capability_plans.append(capability_plan)
        return capability_summaries, capability_plans

    lane_order = [lane["capability"] for lane in lanes]
    summaries_by_capability: dict[str, dict[str, Any]] = {}
    plans_by_capability: dict[str, dict[str, Any]] = {}
    first_error: BaseException | None = None
    with ThreadPoolExecutor(max_workers=min(len(lane_order), max_concurrency)) as pool:
        futures = {
            pool.submit(
                plan_capability,
                project_root,
                run_id,
                objective_id,
                capability,
                objective_outline=objective_outline,
                replace=replace,
                sandbox_mode=sandbox_mode,
                codex_path=codex_path,
                timeout_seconds=timeout_seconds,
                planning_limiter=planning_limiter,
                repair_context=repair_context,
            ): capability
            for capability in lane_order
        }
        for future in as_completed(futures):
            capability = futures[future]
            try:
                summary, plan = future.result()
            except BaseException as exc:  # pragma: no cover - exercised in failure tests via raise below
                if first_error is None:
                    first_error = exc
            else:
                summaries_by_capability[capability] = summary
                plans_by_capability[capability] = plan
    if first_error is not None:
        raise first_error
    return (
        [summaries_by_capability[capability] for capability in lane_order],
        [plans_by_capability[capability] for capability in lane_order],
    )


def plan_capability(
    project_root: Path,
    run_id: str,
    objective_id: str,
    capability: str,
    *,
    objective_outline: dict[str, Any],
    replace: bool,
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    planning_limiter: PlanningLimiter,
    repair_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    phase = read_json(run_dir / "phase-plan.json")["current_phase"]
    result: dict[str, Any] = {
        "events": [],
        "stdout_path": None,
        "stderr_path": None,
        "last_message_path": None,
        "identity_adjustments": {},
        "attempt": 1,
        "recovery_action": None,
    }
    plans_dir = run_dir / "manager-plans"
    output_prefix = f"{phase}-{objective_id}-{capability}"
    plan_path = plans_dir / f"{output_prefix}.json"
    plan = None if replace else load_valid_document(project_root, plan_path, "capability-plan.v1")
    if plan is None:
        recovered_last_message = None
        recovered_last_message_source = None
        if not replace:
            recovered_last_message, recovered_last_message_source = load_latest_valid_planning_last_message(
                project_root,
                plans_dir,
                output_prefix=output_prefix,
                schema_name="capability-plan.v1",
            )
        if recovered_last_message is not None:
            try:
                plan, identity_adjustments = _normalize_and_validate_capability_plan(
                    project_root,
                    run_id,
                    phase,
                    objective_id,
                    capability,
                    objective_outline,
                    sandbox_mode,
                    recovered_last_message,
                )
                result["identity_adjustments"].update(identity_adjustments)
                result["recovery_action"] = (
                    "reused_repaired_last_message_capability_plan"
                    if recovered_last_message_source == "repair"
                    else "reused_last_message_capability_plan"
                )
                write_json(plan_path, plan)
                update_run_file_graph_capability_plan(
                    run_dir,
                    phase=phase,
                    objective_id=objective_id,
                    capability=capability,
                    plan=plan,
                )
            except MANAGER_FIXABLE_PLANNING_ERROR_TYPES as exc:
                prompt_metadata = render_capability_planning_prompt(
                    project_root,
                    run_id,
                    objective_id,
                    capability,
                    objective_outline,
                    ignore_existing_phase_tasks=replace,
                    repair_context=repair_context,
                )
                execution_prompt = build_capability_planning_prompt(
                    (project_root / prompt_metadata["prompt_path"]).read_text(encoding="utf-8")
                )
                result, plan, identity_adjustments = repair_invalid_planning_response(
                    project_root,
                    run_id,
                    phase=phase,
                    activity_id=capability_plan_activity_id(phase, objective_id, capability),
                    kind="capability_plan",
                    entity_id=f"{objective_id}:{capability}",
                    display_name=f"Plan {objective_id}:{capability}",
                    assigned_role=resolve_capability_manager_role(objective_outline, capability),
                    base_prompt_metadata=prompt_metadata,
                    base_execution_prompt=execution_prompt,
                    output_schema_name="capability-plan.response.v1",
                    display_schema_name="capability-plan.v1",
                    output_prefix=output_prefix,
                    failure_label=f"{objective_id}:{capability}",
                    sandbox_mode=sandbox_mode,
                    codex_path=codex_path,
                    timeout_seconds=timeout_seconds,
                    planning_limiter=planning_limiter,
                    previous_payload=recovered_last_message,
                    validation_error=str(exc),
                    normalize_payload=lambda repair_payload: _normalize_and_validate_capability_plan(
                        project_root,
                        run_id,
                        phase,
                        objective_id,
                        capability,
                        objective_outline,
                        sandbox_mode,
                        repair_payload,
                    ),
                    repair_context=repair_context,
                )
                result["identity_adjustments"].update(identity_adjustments)
                write_json(plan_path, plan)
                update_run_file_graph_capability_plan(
                    run_dir,
                    phase=phase,
                    objective_id=objective_id,
                    capability=capability,
                    plan=plan,
                )
        else:
            prompt_metadata = None
            execution_prompt = ""
            current_repair_context = repair_context
            compact_retry_used = False
            while True:
                prompt_metadata = render_capability_planning_prompt(
                    project_root,
                    run_id,
                    objective_id,
                    capability,
                    objective_outline,
                    ignore_existing_phase_tasks=replace,
                    repair_context=current_repair_context,
                )
                execution_prompt = build_capability_planning_prompt(
                    (project_root / prompt_metadata["prompt_path"]).read_text(encoding="utf-8")
                )
                try:
                    result = execute_planning_activity(
                        project_root,
                        run_id,
                        phase=phase,
                        activity_id=capability_plan_activity_id(phase, objective_id, capability),
                        kind="capability_plan",
                        entity_id=f"{objective_id}:{capability}",
                        display_name=f"Plan {objective_id}:{capability}",
                        assigned_role=resolve_capability_manager_role(objective_outline, capability),
                        prompt_metadata=prompt_metadata,
                        execution_prompt=execution_prompt,
                        output_schema_name="capability-plan.response.v1",
                        output_prefix=output_prefix,
                        failure_label=f"{objective_id}:{capability}",
                        sandbox_mode=sandbox_mode,
                        codex_path=codex_path,
                        timeout_seconds=timeout_seconds,
                        planning_limiter=planning_limiter,
                    )
                    if compact_retry_used:
                        result["recovery_action"] = "compact_repair_retry"
                    break
                except ExecutorError as exc:
                    if should_retry_compact_release_repair(
                        exc,
                        current_repair_context,
                        compact_retry_used=compact_retry_used,
                    ):
                        compact_retry_used = True
                        current_repair_context = compact_release_repair_context(current_repair_context)
                        record_event(
                            project_root,
                            run_id,
                            phase=phase,
                            activity_id=capability_plan_activity_id(phase, objective_id, capability),
                            event_type="planning.retry_scheduled",
                            message=(
                                f"Retrying compact repair planning for capability {capability} in objective "
                                f"{objective_id} after a stalled release-repair planning turn."
                            ),
                            payload={
                                "reason": "release_repair_compact_retry",
                                "objective_id": objective_id,
                                "capability": capability,
                            },
                        )
                        continue
                    raise
            try:
                plan, identity_adjustments = _normalize_and_validate_capability_plan(
                    project_root,
                    run_id,
                    phase,
                    objective_id,
                    capability,
                    objective_outline,
                    sandbox_mode,
                    result["payload"],
                )
            except MANAGER_FIXABLE_PLANNING_ERROR_TYPES as exc:
                result, plan, identity_adjustments = repair_invalid_planning_response(
                    project_root,
                    run_id,
                    phase=phase,
                    activity_id=capability_plan_activity_id(phase, objective_id, capability),
                    kind="capability_plan",
                    entity_id=f"{objective_id}:{capability}",
                    display_name=f"Plan {objective_id}:{capability}",
                    assigned_role=resolve_capability_manager_role(objective_outline, capability),
                    base_prompt_metadata=prompt_metadata,
                    base_execution_prompt=execution_prompt,
                    output_schema_name="capability-plan.response.v1",
                    display_schema_name="capability-plan.v1",
                    output_prefix=output_prefix,
                    failure_label=f"{objective_id}:{capability}",
                    sandbox_mode=sandbox_mode,
                    codex_path=codex_path,
                    timeout_seconds=timeout_seconds,
                    planning_limiter=planning_limiter,
                    previous_payload=result["payload"],
                    validation_error=str(exc),
                    normalize_payload=lambda repair_payload: _normalize_and_validate_capability_plan(
                        project_root,
                        run_id,
                        phase,
                        objective_id,
                        capability,
                        objective_outline,
                        sandbox_mode,
                        repair_payload,
                    ),
                    repair_context=repair_context,
                )
            result["identity_adjustments"].update(identity_adjustments)
            write_json(plan_path, plan)
            update_run_file_graph_capability_plan(
                run_dir,
                phase=phase,
                objective_id=objective_id,
                capability=capability,
                plan=plan,
            )
    else:
        plan, identity_adjustments = normalize_capability_plan(
            project_root,
            plan,
            run_id=run_id,
            phase=phase,
            objective_id=objective_id,
            capability=capability,
            objective_outline=objective_outline,
            default_sandbox_mode=sandbox_mode,
        )
        result["identity_adjustments"].update(identity_adjustments)
        result["recovery_action"] = "reused_valid_capability_plan"
        write_json(plan_path, plan)
        update_run_file_graph_capability_plan(
            run_dir,
            phase=phase,
            objective_id=objective_id,
            capability=capability,
            plan=plan,
        )
    summary = {
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "capability": capability,
        "thread_id": extract_thread_id(result["events"]),
        "usage": extract_usage(result["events"]),
        "plan_path": f"runs/{run_id}/manager-plans/{phase}-{objective_id}-{capability}.json",
        "task_ids": [task["task_id"] for task in plan["tasks"]],
        "bundle_ids": [bundle["bundle_id"] for bundle in plan["bundle_plan"]],
        "handoff_ids": [handoff["handoff_id"] for handoff in plan.get("collaboration_handoffs", [])],
        "stdout_path": result["stdout_path"],
        "stderr_path": result["stderr_path"],
        "last_message_path": result["last_message_path"],
        "identity_adjustments": result["identity_adjustments"],
        "attempt": result["attempt"],
        "recovery_action": result["recovery_action"],
    }
    write_json(run_dir / "manager-plans" / f"{phase}-{objective_id}-{capability}.summary.json", summary)
    activity_status = "recovered" if result["attempt"] > 1 or result["recovery_action"] else "completed"
    ensure_activity(
        project_root,
        run_id,
        activity_id=capability_plan_activity_id(phase, objective_id, capability),
        kind="capability_plan",
        entity_id=f"{objective_id}:{capability}",
        phase=phase,
        objective_id=objective_id,
        display_name=f"Plan {objective_id}:{capability}",
        assigned_role=resolve_capability_manager_role(objective_outline, capability),
        status=activity_status,
        progress_stage=activity_status,
        current_activity=plan["summary"],
        stdout_path=result["stdout_path"],
        stderr_path=result["stderr_path"],
        output_path=f"runs/{run_id}/manager-plans/{phase}-{objective_id}-{capability}.json",
        process_metadata=None,
        recovered_at=now_timestamp() if activity_status == "recovered" else None,
        recovery_action=result["recovery_action"],
    )
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=capability_plan_activity_id(phase, objective_id, capability),
        event_type="planning.completed",
        message=f"Planning activity for capability {capability} in objective {objective_id} completed.",
        payload={
            "plan_path": f"runs/{run_id}/manager-plans/{phase}-{objective_id}-{capability}.json",
            "attempt": result["attempt"],
            "recovery_action": result["recovery_action"],
        },
    )
    return summary, plan


def execute_planning_activity(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    activity_id: str,
    kind: str,
    entity_id: str,
    display_name: str,
    assigned_role: str,
    prompt_metadata: dict[str, Any],
    execution_prompt: str,
    output_schema_name: str,
    output_prefix: str,
    failure_label: str,
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    planning_limiter: PlanningLimiter,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    plans_dir = ensure_dir(run_dir / "manager-plans")
    output_schema_path = project_root / "orchestrator" / "schemas" / f"{output_schema_name}.json"
    last_message_path = plans_dir / f"{output_prefix}.last-message.json"
    output_path = plans_dir / f"{output_prefix}.json"
    command = build_codex_command(
        codex_path=codex_path,
        working_directory=project_root,
        output_schema_path=output_schema_path,
        last_message_path=last_message_path,
        sandbox_mode=sandbox_mode,
        additional_directories=[],
    )
    previous_activity = prepare_activity_retry(
        project_root,
        run_id,
        activity_id,
        reason="Starting a new planning attempt.",
    )
    attempt = (int(previous_activity.get("attempt", 1)) + 1) if previous_activity is not None else 1
    stream_attempt = 1
    stdout_path = plans_dir / planning_attempt_stream_name(output_prefix, stream_attempt, "stdout.jsonl")
    stderr_path = plans_dir / planning_attempt_stream_name(output_prefix, stream_attempt, "stderr.log")
    clear_text(stdout_path)
    clear_text(stderr_path)
    timeout_policy = resolve_planning_timeout_policy(phase, timeout_seconds)
    prompt_observability = prompt_metrics(execution_prompt)
    activity_state = ensure_activity(
        project_root,
        run_id,
        activity_id=activity_id,
        kind=kind,
        entity_id=entity_id,
        phase=phase,
        objective_id=entity_id.split(":", 1)[0] if ":" in entity_id else entity_id,
        display_name=display_name,
        assigned_role=assigned_role,
        status="prompt_rendered",
        progress_stage="prompt_rendered",
        current_activity="Rendered planning prompt.",
        prompt_path=prompt_metadata["prompt_path"],
        stdout_path=str(stdout_path.relative_to(project_root)),
        stderr_path=str(stderr_path.relative_to(project_root)),
        output_path=str(output_path.relative_to(project_root)),
        runner_id="codex",
        observability=prompt_observability,
        attempt=attempt,
        begin_attempt=previous_activity is not None,
    )

    def rotate_stream_paths(next_stream_attempt: int) -> None:
        nonlocal stream_attempt, stdout_path, stderr_path
        stream_attempt = next_stream_attempt
        stdout_path = plans_dir / planning_attempt_stream_name(output_prefix, stream_attempt, "stdout.jsonl")
        stderr_path = plans_dir / planning_attempt_stream_name(output_prefix, stream_attempt, "stderr.log")
        clear_text(stdout_path)
        clear_text(stderr_path)
        update_activity(
            project_root,
            run_id,
            activity_id,
            stdout_path=str(stdout_path.relative_to(project_root)),
            stderr_path=str(stderr_path.relative_to(project_root)),
        )

    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=activity_id,
        event_type="planning.prompt_rendered",
        message=f"Rendered planning prompt for {failure_label}.",
        payload={"prompt_path": prompt_metadata["prompt_path"]},
    )
    queue_position, queue_wait_ms = planning_limiter.acquire()
    try:
        if queue_position is not None:
            update_activity(
                project_root,
                run_id,
                activity_id,
                status="queued",
                progress_stage="queued",
                current_activity="Waiting for planning slot.",
                queue_position=queue_position,
                observability={"queue_wait_ms": queue_wait_ms},
            )
            record_event(
                project_root,
                run_id,
                phase=phase,
                activity_id=activity_id,
                event_type="planning.queued",
                message=f"Queued planning activity for {failure_label}.",
                payload={"entity_id": entity_id, "queue_position": queue_position, "queue_wait_ms": queue_wait_ms},
            )
        update_activity(
            project_root,
            run_id,
            activity_id,
            status="launching",
            progress_stage="launching",
            current_activity="Launching planning manager.",
            queue_position=None,
        )
        record_event(
            project_root,
            run_id,
            phase=phase,
            activity_id=activity_id,
            event_type="planning.launching",
            message=f"Launching planning activity for {failure_label}.",
            payload={"entity_id": entity_id},
        )

        planning_progress = {
            "process_started_at_monotonic": None,
            "thread_started_at_monotonic": None,
            "turn_started_at_monotonic": None,
            "last_stream_activity_at_monotonic": None,
        }
        stall_timeout_seconds = planning_stall_timeout_seconds(timeout_policy.timeout_seconds)

        def on_stdout_line(raw_line: str) -> None:
            activity_at = time.monotonic()
            planning_progress["last_stream_activity_at_monotonic"] = activity_at
            try:
                event_payload = json.loads(raw_line)
            except json.JSONDecodeError:
                event_payload = None
            if isinstance(event_payload, dict):
                event_type = event_payload.get("type")
                if event_type == "thread.started":
                    planning_progress["thread_started_at_monotonic"] = activity_at
                elif event_type == "turn.started":
                    planning_progress["turn_started_at_monotonic"] = activity_at
            append_text(stdout_path, raw_line + "\n")
            note_activity_stream(
                project_root,
                run_id,
                activity_id,
                stdout_bytes=len((raw_line + "\n").encode("utf-8")),
            )
            handle_codex_event_line(project_root, run_id, phase, activity_id, raw_line)

        def on_stderr_line(raw_line: str) -> None:
            planning_progress["last_stream_activity_at_monotonic"] = time.monotonic()
            append_text(stderr_path, raw_line)
            note_activity_stream(
                project_root,
                run_id,
                activity_id,
                stderr_bytes=len(raw_line.encode("utf-8")),
            )

        def on_process_started(process: subprocess.Popen[str]) -> None:
            planning_progress["process_started_at_monotonic"] = time.monotonic()
            update_activity(
                project_root,
                run_id,
                activity_id,
                process_metadata={
                    "pid": process.pid,
                    "started_at": activity_state["updated_at"],
                    "command": " ".join(command),
                    "cwd": str(project_root),
                },
            )

        def stall_reason() -> str | None:
            last_activity_at = planning_progress["last_stream_activity_at_monotonic"]
            now_monotonic = time.monotonic()
            turn_started_at = planning_progress["turn_started_at_monotonic"]
            thread_started_at = planning_progress["thread_started_at_monotonic"]
            process_started_at = planning_progress["process_started_at_monotonic"]
            if turn_started_at is not None and last_activity_at is not None and now_monotonic - last_activity_at >= stall_timeout_seconds:
                return "stall_after_turn_started"
            if thread_started_at is not None and last_activity_at is not None and now_monotonic - last_activity_at >= stall_timeout_seconds:
                return "stall_after_thread_started"
            if process_started_at is not None and last_activity_at is None and now_monotonic - process_started_at >= stall_timeout_seconds:
                return "stall_before_first_output"
            return None

        stdout_attempts: list[str] = []
        stderr_attempts: list[str] = []
        missing_final_message_retry_used = False
        stall_retry_used = False
        total_attempts = timeout_policy.max_timeout_retries + 1
        missing_final_message_attempt = 0
        final_response: str | None = None
        while final_response is None:
            completed = None
            call_started_at = now_timestamp()
            call_completed_at = now_timestamp()
            call_latency_ms = 0
            for timeout_attempt in range(1, total_attempts + 1):
                call_started_at = now_timestamp()
                call_started_monotonic = time.monotonic()
                try:
                    completed = run_codex_command(
                        command,
                        prompt=execution_prompt,
                        cwd=project_root,
                        env=build_exec_environment(project_root / "runs" / run_id / activity_id),
                        timeout_seconds=timeout_policy.timeout_seconds,
                        on_stdout_line=on_stdout_line,
                        on_stderr_line=on_stderr_line,
                        on_process_started=on_process_started,
                        stall_timeout_seconds=stall_timeout_seconds,
                        stall_reason=stall_reason,
                    )
                    call_completed_at = now_timestamp()
                    call_latency_ms = int((time.monotonic() - call_started_monotonic) * 1000)
                    break
                except CodexProcessStall as exc:
                    stall_stdout = coerce_process_text(exc.output)
                    stall_stderr = coerce_process_text(exc.stderr)
                    if stall_stdout:
                        append_text(stdout_path, stall_stdout)
                        if not stall_stdout.endswith("\n"):
                            append_text(stdout_path, "\n")
                    if stall_stderr:
                        append_text(stderr_path, stall_stderr)
                    stdout_attempts.append(stall_stdout)
                    stderr_attempts.append(stall_stderr)
                    call_completed_at = now_timestamp()
                    call_latency_ms = int((time.monotonic() - call_started_monotonic) * 1000)
                    current_activity = read_activity(project_root, run_id, activity_id)
                    record_llm_call(
                        project_root,
                        run_id,
                        phase=phase,
                        activity_id=activity_id,
                        kind=kind,
                        attempt=attempt,
                        started_at=call_started_at,
                        completed_at=call_completed_at,
                        latency_ms=call_latency_ms,
                        queue_wait_ms=int((current_activity.get("observability", {}) or {}).get("queue_wait_ms", 0)),
                        prompt_char_count=prompt_observability["prompt_char_count"],
                        prompt_line_count=prompt_observability["prompt_line_count"],
                        prompt_bytes=prompt_observability["prompt_bytes"],
                        timed_out=False,
                        retry_scheduled=timeout_attempt <= timeout_policy.max_timeout_retries,
                        success=False,
                        input_tokens=0,
                        cached_input_tokens=0,
                        output_tokens=0,
                        stdout_bytes=len(stall_stdout.encode("utf-8")),
                        stderr_bytes=len(stall_stderr.encode("utf-8")),
                        timeout_seconds=timeout_policy.timeout_seconds,
                        error=exc.reason,
                        label=failure_label,
                    )
                    update_activity(
                        project_root,
                        run_id,
                        activity_id,
                        observability=accumulate_planning_observability(
                            current_activity["observability"],
                            latency_ms=call_latency_ms,
                            stdout_bytes=len(stall_stdout.encode("utf-8")),
                            stderr_bytes=len(stall_stderr.encode("utf-8")),
                            timed_out=False,
                            timeout_retry_scheduled=False,
                        ),
                    )
                    record_event(
                        project_root,
                        run_id,
                        phase=phase,
                        activity_id=activity_id,
                        event_type="planning.stall_detected",
                        message=f"Planning activity for {failure_label} stalled.",
                        payload={
                            "reason": exc.reason,
                            "stall_timeout_seconds": exc.stall_seconds,
                        },
                    )
                    if timeout_attempt <= timeout_policy.max_timeout_retries:
                        stall_retry_used = True
                        message = (
                            f"Planning activity for {failure_label} stalled after {exc.stall_seconds} seconds "
                            f"({exc.reason}); retrying ({timeout_attempt}/{total_attempts})."
                        )
                        update_activity(
                            project_root,
                            run_id,
                            activity_id,
                            status="recovering",
                            progress_stage="recovering",
                            current_activity=message,
                            status_reason="stall_retry_scheduled",
                            process_metadata=None,
                        )
                        record_event(
                            project_root,
                            run_id,
                            phase=phase,
                            activity_id=activity_id,
                            event_type="planning.retry_scheduled",
                            message=message,
                            payload={
                                "reason": exc.reason,
                                "stall_timeout_seconds": exc.stall_seconds,
                                "attempt": timeout_attempt,
                                "max_attempts": total_attempts,
                            },
                        )
                        rotate_stream_paths(stream_attempt + 1)
                        continue
                    failure_message = (
                        f"Planning activity for {failure_label} stalled after {exc.stall_seconds} seconds "
                        f"({exc.reason}); resume-phase is recommended."
                    )
                    update_activity(
                        project_root,
                        run_id,
                        activity_id,
                        status="failed",
                        progress_stage="failed",
                        current_activity=failure_message,
                        status_reason="planning_stalled",
                        process_metadata=None,
                    )
                    record_event(
                        project_root,
                        run_id,
                        phase=phase,
                        activity_id=activity_id,
                        event_type="planning.failed",
                        message=failure_message,
                        payload={
                            "reason": exc.reason,
                            "stall_timeout_seconds": exc.stall_seconds,
                            "attempts": total_attempts,
                        },
                    )
                    raise ExecutorError(failure_message) from exc
                except subprocess.TimeoutExpired as exc:
                    timeout_stdout = coerce_process_text(exc.stdout)
                    timeout_stderr = coerce_process_text(exc.stderr)
                    if timeout_stdout:
                        append_text(stdout_path, timeout_stdout)
                        if not timeout_stdout.endswith("\n"):
                            append_text(stdout_path, "\n")
                    if timeout_stderr:
                        append_text(stderr_path, timeout_stderr)
                    stdout_attempts.append(coerce_process_text(exc.stdout))
                    stderr_attempts.append(coerce_process_text(exc.stderr))
                    call_completed_at = now_timestamp()
                    call_latency_ms = int((time.monotonic() - call_started_monotonic) * 1000)
                    current_activity = read_activity(project_root, run_id, activity_id)
                    record_llm_call(
                        project_root,
                        run_id,
                        phase=phase,
                        activity_id=activity_id,
                        kind=kind,
                        attempt=attempt,
                        started_at=call_started_at,
                        completed_at=call_completed_at,
                        latency_ms=call_latency_ms,
                        queue_wait_ms=int((current_activity.get("observability", {}) or {}).get("queue_wait_ms", 0)),
                        prompt_char_count=prompt_observability["prompt_char_count"],
                        prompt_line_count=prompt_observability["prompt_line_count"],
                        prompt_bytes=prompt_observability["prompt_bytes"],
                        timed_out=True,
                        retry_scheduled=timeout_attempt <= timeout_policy.max_timeout_retries,
                        success=False,
                        input_tokens=0,
                        cached_input_tokens=0,
                        output_tokens=0,
                        stdout_bytes=len(timeout_stdout.encode("utf-8")),
                        stderr_bytes=len(timeout_stderr.encode("utf-8")),
                        timeout_seconds=timeout_policy.timeout_seconds,
                        error="timeout",
                        label=failure_label,
                    )
                    update_activity(
                        project_root,
                        run_id,
                        activity_id,
                        observability=accumulate_planning_observability(
                            current_activity["observability"],
                            latency_ms=call_latency_ms,
                            stdout_bytes=len(timeout_stdout.encode("utf-8")),
                            stderr_bytes=len(timeout_stderr.encode("utf-8")),
                            timed_out=True,
                            timeout_retry_scheduled=timeout_attempt <= timeout_policy.max_timeout_retries,
                        ),
                    )
                    if timeout_attempt <= timeout_policy.max_timeout_retries:
                        message = timeout_retry_message(
                            "planning",
                            failure_label,
                            timeout_seconds=timeout_policy.timeout_seconds,
                            attempt=timeout_attempt,
                            max_attempts=total_attempts,
                        )
                        update_activity(
                            project_root,
                            run_id,
                            activity_id,
                            status="recovering",
                            progress_stage="recovering",
                            current_activity=message,
                            status_reason="timeout_retry_scheduled",
                            process_metadata=None,
                        )
                        record_event(
                            project_root,
                            run_id,
                            phase=phase,
                            activity_id=activity_id,
                            event_type="planning.timeout_retry_scheduled",
                            message=message,
                            payload={
                                "timeout_seconds": timeout_policy.timeout_seconds,
                                "attempt": timeout_attempt,
                                "max_attempts": total_attempts,
                            },
                        )
                        rotate_stream_paths(stream_attempt + 1)
                        continue
                    failure_message = timeout_final_message(
                        "planning",
                        failure_label,
                        timeout_seconds=timeout_policy.timeout_seconds,
                        attempts=total_attempts,
                        resume_recommended=True,
                        explicit_override=timeout_policy.source == "explicit",
                    )
                    update_activity(
                        project_root,
                        run_id,
                        activity_id,
                        status="failed",
                        progress_stage="failed",
                        current_activity=failure_message,
                        status_reason="timeout_exhausted",
                        process_metadata=None,
                    )
                    record_event(
                        project_root,
                        run_id,
                        phase=phase,
                        activity_id=activity_id,
                        event_type="planning.failed",
                        message=failure_message,
                        payload={"timeout_seconds": timeout_policy.timeout_seconds, "attempts": total_attempts},
                    )
                    raise ExecutorError(failure_message) from exc

            assert completed is not None
            stdout_attempts.append(completed.stdout)
            stderr_attempts.append(completed.stderr)
            events = parse_jsonl_events(completed.stdout)
            usage = extract_usage(events) or {}
            current_activity = read_activity(project_root, run_id, activity_id)
            record_llm_call(
                project_root,
                run_id,
                phase=phase,
                activity_id=activity_id,
                kind=kind,
                attempt=attempt,
                started_at=call_started_at,
                completed_at=call_completed_at,
                latency_ms=call_latency_ms,
                queue_wait_ms=int((current_activity.get("observability", {}) or {}).get("queue_wait_ms", 0)),
                prompt_char_count=prompt_observability["prompt_char_count"],
                prompt_line_count=prompt_observability["prompt_line_count"],
                prompt_bytes=prompt_observability["prompt_bytes"],
                timed_out=False,
                retry_scheduled=False,
                success=completed.returncode == 0 and extract_turn_failure(events) is None,
                input_tokens=int(usage.get("input_tokens", 0)),
                cached_input_tokens=int(usage.get("cached_input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                stdout_bytes=len(completed.stdout.encode("utf-8")),
                stderr_bytes=len(completed.stderr.encode("utf-8")),
                timeout_seconds=timeout_policy.timeout_seconds,
                error=extract_turn_failure(events),
                label=failure_label,
            )
            update_activity(
                project_root,
                run_id,
                activity_id,
                observability=accumulate_planning_observability(
                    current_activity["observability"],
                    latency_ms=call_latency_ms,
                    input_tokens=int(usage.get("input_tokens", 0)),
                    cached_input_tokens=int(usage.get("cached_input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                    stdout_bytes=len(completed.stdout.encode("utf-8")),
                    stderr_bytes=len(completed.stderr.encode("utf-8")),
                    timed_out=False,
                    timeout_retry_scheduled=False,
                ),
            )
            failure = extract_turn_failure(events)
            if completed.returncode != 0 or failure is not None:
                message = failure or completed.stderr.strip() or f"codex exec exited with code {completed.returncode}"
                update_activity(
                    project_root,
                    run_id,
                    activity_id,
                    status="failed",
                    progress_stage="failed",
                    current_activity=message,
                    process_metadata=None,
                )
                record_event(
                    project_root,
                    run_id,
                    phase=phase,
                    activity_id=activity_id,
                    event_type="planning.failed",
                    message=f"Planning activity for {failure_label} failed.",
                    payload={"error": message},
                )
                raise ExecutorError(message)
            try:
                final_response = extract_final_response(events)
            except ExecutorError as exc:
                missing_final_message_attempt += 1
                if missing_final_message_attempt <= MAX_MANAGER_PLANNING_MISSING_FINAL_MESSAGE_RETRIES:
                    missing_final_message_retry_used = True
                    message = (
                        f"Planning activity for {failure_label} produced no final agent message; retrying "
                        f"({missing_final_message_attempt}/{MAX_MANAGER_PLANNING_MISSING_FINAL_MESSAGE_RETRIES + 1})."
                    )
                    update_activity(
                        project_root,
                        run_id,
                        activity_id,
                        status="recovering",
                        progress_stage="recovering",
                        current_activity=message,
                        status_reason="missing_final_message_retry_scheduled",
                        process_metadata=None,
                    )
                    record_event(
                        project_root,
                        run_id,
                        phase=phase,
                        activity_id=activity_id,
                        event_type="planning.retry_scheduled",
                        message=message,
                        payload={
                            "reason": "missing_final_agent_message",
                            "attempt": missing_final_message_attempt,
                            "max_attempts": MAX_MANAGER_PLANNING_MISSING_FINAL_MESSAGE_RETRIES + 1,
                        },
                    )
                    rotate_stream_paths(stream_attempt + 1)
                    continue
                update_activity(
                    project_root,
                    run_id,
                    activity_id,
                    status="failed",
                    progress_stage="failed",
                    current_activity=str(exc),
                    process_metadata=None,
                )
                record_event(
                    project_root,
                    run_id,
                    phase=phase,
                    activity_id=activity_id,
                    event_type="planning.failed",
                    message=f"Planning activity for {failure_label} failed.",
                    payload={"error": str(exc), "reason": "missing_final_agent_message"},
                )
                raise
        try:
            payload = json.loads(final_response)
        except json.JSONDecodeError as exc:
            update_activity(
                project_root,
                run_id,
                activity_id,
                status="failed",
                progress_stage="failed",
                current_activity="Planning response was not valid JSON.",
                process_metadata=None,
            )
            raise ExecutorError(f"Planning response was not valid JSON: {final_response}") from exc
        payload = strip_planner_managed_fields(payload)
        write_json(last_message_path, payload)
    finally:
        planning_limiter.release()

    return {
        "payload": payload,
        "events": events,
        "stdout_path": str(stdout_path.relative_to(project_root)),
        "stderr_path": str(stderr_path.relative_to(project_root)),
        "last_message_path": str(last_message_path.relative_to(project_root)),
        "identity_adjustments": {},
        "attempt": attempt,
        "recovery_action": (
            "retry"
            if previous_activity is not None
            else (
                "missing_final_message_retry"
                if missing_final_message_retry_used
                else ("stall_retry" if stall_retry_used else ("timeout_retry" if len(stdout_attempts) > 1 else None))
            )
        ),
    }


def build_planning_repair_prompt(
    base_execution_prompt: str,
    *,
    output_schema_name: str,
    display_schema_name: str | None,
    previous_payload: dict[str, Any],
    validation_error: str,
    repair_context: dict[str, Any] | None = None,
) -> str:
    rendered_schema_name = str(display_schema_name or output_schema_name)
    relevant_payload = planning_repair_payload_slice(
        previous_payload,
        validation_error=validation_error,
        repair_context=repair_context,
    )
    repair_header = [
        "# Repair Assignment",
        "",
        "You are repairing a previously returned planning response.",
        "The previous response was not accepted because it failed deterministic validation.",
        "",
        f"Your job in this turn is to redo the same planning turn while correcting the invalid parts of the previous response.",
        "Preserve as much of the previous valid plan as possible.",
        "Do not broaden scope unless a change is required to make the response valid.",
        "Do not redesign the plan from scratch unless the validation error makes that unavoidable.",
        "",
        f"Return exactly one corrected `{rendered_schema_name}` JSON object.",
        "Return JSON only.",
        "",
        "# What Failed In The Previous Response",
        "",
        f"- Schema: `{rendered_schema_name}`",
        f"- Validation error: {validation_error}",
    ]
    rejection_reasons = [
        str(value).strip()
        for value in (repair_context or {}).get("rejection_reasons", [])
        if isinstance(value, str) and str(value).strip()
    ]
    if rejection_reasons:
        repair_header.extend(["", "Additional repair constraints:"])
        repair_header.extend(f"- {value}" for value in rejection_reasons)
    repair_header.extend(
        [
            "",
            "# How To Use This Repair Prompt",
            "",
            "This repair prompt contains:",
            "- the previous invalid response slice, which shows the part of the old response that needs correction",
            "- the planning prompt below, which is still the source of truth for the objective, scope, and response contract",
            "",
            "Use the previous invalid response slice to preserve valid content where possible.",
            "Use the planning prompt below to decide what the corrected response must look like.",
            "If the previous invalid response conflicts with the planning prompt below, follow the planning prompt below.",
            "",
            "# Previous Invalid Response Slice",
            "",
            "```json",
            json.dumps(relevant_payload, indent=2, sort_keys=True),
            "```",
            "",
            "# Planning Prompt To Redo",
        ]
    )
    return "\n".join(repair_header) + "\n\n" + base_execution_prompt


def write_planning_repair_prompt_metadata(
    project_root: Path,
    run_id: str,
    *,
    output_prefix: str,
    base_metadata: dict[str, Any],
    repair_attempt: int,
    prompt_text: str,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    prompt_path = run_dir / "manager-plans" / f"{output_prefix}.repair-{repair_attempt}.prompt.md"
    log_path = run_dir / "manager-plans" / f"{output_prefix}.repair-{repair_attempt}.prompt.json"
    write_text(prompt_path, prompt_text)
    prompt_stats = prompt_metrics(prompt_text)
    metadata = dict(base_metadata)
    metadata.update(
        {
            "prompt_path": str(prompt_path.relative_to(project_root)),
            "repair_attempt": repair_attempt,
            "repair_for_prompt_path": base_metadata.get("prompt_path"),
            "prompt_char_count": prompt_stats["prompt_char_count"],
            "prompt_line_count": prompt_stats["prompt_line_count"],
        }
    )
    write_json(log_path, metadata)
    return metadata


def repair_invalid_planning_response(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    activity_id: str,
    kind: str,
    entity_id: str,
    display_name: str,
    assigned_role: str,
    base_prompt_metadata: dict[str, Any],
    base_execution_prompt: str,
    output_schema_name: str,
    display_schema_name: str | None,
    output_prefix: str,
    failure_label: str,
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    planning_limiter: PlanningLimiter,
    previous_payload: dict[str, Any],
    validation_error: str,
    normalize_payload: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, dict[str, str]]]],
    validate_plan: Callable[[dict[str, Any]], None] | None = None,
    repair_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, str]]]:
    last_error = validation_error
    last_payload = previous_payload
    for repair_attempt in range(1, MAX_MANAGER_PLANNING_REPAIR_ATTEMPTS + 1):
        repair_prompt = build_planning_repair_prompt(
            base_execution_prompt,
            output_schema_name=output_schema_name,
            display_schema_name=display_schema_name,
            previous_payload=last_payload,
            validation_error=last_error,
            repair_context=repair_context,
        )
        repair_metadata = write_planning_repair_prompt_metadata(
            project_root,
            run_id,
            output_prefix=output_prefix,
            base_metadata=base_prompt_metadata,
            repair_attempt=repair_attempt,
            prompt_text=repair_prompt,
        )
        record_event(
            project_root,
            run_id,
            phase=phase,
            activity_id=activity_id,
            event_type="planning.repair_requested",
            message=f"Retrying planning for {failure_label} after validation failure.",
            payload={"repair_attempt": repair_attempt, "error": last_error},
        )
        repair_result = execute_planning_activity(
            project_root,
            run_id,
            phase=phase,
            activity_id=activity_id,
            kind=kind,
            entity_id=entity_id,
            display_name=display_name,
            assigned_role=assigned_role,
            prompt_metadata=repair_metadata,
            execution_prompt=repair_prompt,
            output_schema_name=output_schema_name,
            output_prefix=f"{output_prefix}.repair-{repair_attempt}",
            failure_label=f"{failure_label} (repair {repair_attempt})",
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            planning_limiter=planning_limiter,
        )
        try:
            plan, identity_adjustments = normalize_payload(repair_result["payload"])
            if validate_plan is not None:
                validate_plan(plan)
        except MANAGER_FIXABLE_PLANNING_ERROR_TYPES as exc:
            last_error = str(exc)
            last_payload = repair_result["payload"]
            continue
        repair_result["recovery_action"] = "planning_repair"
        record_event(
            project_root,
            run_id,
            phase=phase,
            activity_id=activity_id,
            event_type="planning.repair_completed",
            message=f"Planning repair for {failure_label} succeeded.",
            payload={"repair_attempt": repair_attempt},
        )
        return repair_result, plan, identity_adjustments
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=activity_id,
        event_type="planning.repair_failed",
        message=f"Planning repair for {failure_label} failed.",
        payload={"attempts": MAX_MANAGER_PLANNING_REPAIR_ATTEMPTS, "error": last_error},
    )
    raise ExecutorError(last_error)


def build_post_validation_repair_context(source: str, error: str) -> dict[str, Any]:
    return {
        "source": source,
        "reason": "The previous plan failed deterministic validation after aggregation. Repair only the exact issues below.",
        "rejection_reasons": [error],
    }


def extract_task_id_from_planning_error(message: str) -> str | None:
    patterns = [
        r"for task ([A-Za-z0-9._:-]+):",
        r"task ([A-Za-z0-9._:-]+) ",
        r"task id ([A-Za-z0-9._:-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            task_id = match.group(1).strip()
            if task_id:
                return task_id
    return None


def capability_for_task_id(capability_plans: list[dict[str, Any]], task_id: str) -> str | None:
    for plan in capability_plans:
        for task in plan.get("tasks", []):
            if task.get("task_id") == task_id:
                capability = str(plan.get("capability") or task.get("capability") or "").strip()
                if capability:
                    return capability
    return None


def rewrite_capability_summary(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    objective_id: str,
    objective_outline: dict[str, Any],
    capability: str,
    result: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    summary = {
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "capability": capability,
        "thread_id": extract_thread_id(result["events"]),
        "usage": extract_usage(result["events"]),
        "plan_path": f"runs/{run_id}/manager-plans/{phase}-{objective_id}-{capability}.json",
        "task_ids": [task["task_id"] for task in plan["tasks"]],
        "bundle_ids": [bundle["bundle_id"] for bundle in plan["bundle_plan"]],
        "handoff_ids": [handoff["handoff_id"] for handoff in plan.get("collaboration_handoffs", [])],
        "stdout_path": result["stdout_path"],
        "stderr_path": result["stderr_path"],
        "last_message_path": result["last_message_path"],
        "identity_adjustments": result["identity_adjustments"],
        "attempt": result["attempt"],
        "recovery_action": result["recovery_action"],
    }
    run_dir = project_root / "runs" / run_id
    write_json(run_dir / "manager-plans" / f"{phase}-{objective_id}-{capability}.summary.json", summary)
    ensure_activity(
        project_root,
        run_id,
        activity_id=capability_plan_activity_id(phase, objective_id, capability),
        kind="capability_plan",
        entity_id=f"{objective_id}:{capability}",
        phase=phase,
        objective_id=objective_id,
        display_name=f"Plan {objective_id}:{capability}",
        assigned_role=resolve_capability_manager_role(objective_outline, capability),
        status="recovered",
        progress_stage="recovered",
        current_activity=plan["summary"],
        output_path=f"runs/{run_id}/manager-plans/{phase}-{objective_id}-{capability}.json",
        process_metadata=None,
        recovered_at=now_timestamp(),
        recovery_action=result["recovery_action"],
    )
    return summary


def repair_capability_plan_after_validation_error(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    objective_id: str,
    capability: str,
    objective_outline: dict[str, Any],
    objective: dict[str, Any],
    capability_plans: list[dict[str, Any]],
    validation_error: str,
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    planning_limiter: PlanningLimiter,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt_metadata = render_capability_planning_prompt(
        project_root,
        run_id,
        objective_id,
        capability,
        objective_outline,
        ignore_existing_phase_tasks=True,
        repair_context=build_post_validation_repair_context("post-aggregation-capability-validation", validation_error),
    )
    execution_prompt = build_capability_planning_prompt(
        (project_root / prompt_metadata["prompt_path"]).read_text(encoding="utf-8")
    )
    previous_payload = next(plan for plan in capability_plans if str(plan.get("capability")) == capability)
    lane_order = [str(lane.get("capability") or "").strip() for lane in objective_outline.get("capability_lanes", [])]

    def validate_repaired(repaired_plan: dict[str, Any]) -> None:
        updated_plans = []
        for lane_capability in lane_order:
            if lane_capability == capability:
                updated_plans.append(repaired_plan)
            else:
                updated_plans.append(next(plan for plan in capability_plans if str(plan.get("capability")) == lane_capability))
        objective_plan = aggregate_capability_plans(
            project_root,
            run_id,
            phase,
            objective_id,
            objective_outline,
            updated_plans,
        )
        validate_objective_plan_contents(project_root, objective_plan, objective)
        validate_planned_task_inputs(project_root, run_id, objective_plan["phase"], objective_plan["objective_id"], objective_plan["tasks"])

    result, repaired_plan, identity_adjustments = repair_invalid_planning_response(
        project_root,
        run_id,
        phase=phase,
        activity_id=capability_plan_activity_id(phase, objective_id, capability),
        kind="capability_plan",
        entity_id=f"{objective_id}:{capability}",
        display_name=f"Plan {objective_id}:{capability}",
        assigned_role=resolve_capability_manager_role(objective_outline, capability),
        base_prompt_metadata=prompt_metadata,
        base_execution_prompt=execution_prompt,
        output_schema_name="capability-plan.response.v1",
        display_schema_name="capability-plan.v1",
        output_prefix=f"{phase}-{objective_id}-{capability}",
        failure_label=f"{objective_id}:{capability}",
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        timeout_seconds=timeout_seconds,
        planning_limiter=planning_limiter,
        previous_payload=previous_payload,
        validation_error=validation_error,
        normalize_payload=lambda repair_payload: normalize_capability_plan(
            project_root,
            repair_payload,
            run_id=run_id,
            phase=phase,
            objective_id=objective_id,
            capability=capability,
            objective_outline=objective_outline,
            default_sandbox_mode=sandbox_mode,
        ),
        validate_plan=validate_repaired,
    )
    result["identity_adjustments"].update(identity_adjustments)
    plan_path = project_root / "runs" / run_id / "manager-plans" / f"{phase}-{objective_id}-{capability}.json"
    write_json(plan_path, repaired_plan)
    update_run_file_graph_capability_plan(
        project_root / "runs" / run_id,
        phase=phase,
        objective_id=objective_id,
        capability=capability,
        plan=repaired_plan,
    )
    summary = rewrite_capability_summary(
        project_root,
        run_id,
        phase=phase,
        objective_id=objective_id,
        objective_outline=objective_outline,
        capability=capability,
        result=result,
        plan=repaired_plan,
    )
    return summary, repaired_plan


def repair_objective_outline_after_validation_error(
    project_root: Path,
    run_id: str,
    *,
    phase: str,
    objective_id: str,
    objective: dict[str, Any],
    validation_error: str,
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int | None,
    planning_limiter: PlanningLimiter,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, str]]]:
    prompt_metadata = render_objective_planning_prompt(
        project_root,
        run_id,
        objective_id,
        ignore_existing_phase_tasks=True,
        repair_context=build_post_validation_repair_context("post-aggregation-objective-validation", validation_error),
    )
    execution_prompt = build_planning_prompt(
        (project_root / prompt_metadata["prompt_path"]).read_text(encoding="utf-8")
    )
    previous_payload = read_json(project_root / "runs" / run_id / "manager-plans" / f"{phase}-{objective_id}.outline.json")
    return repair_invalid_planning_response(
        project_root,
        run_id,
        phase=phase,
        activity_id=plan_activity_id(phase, objective_id),
        kind="objective_plan",
        entity_id=objective_id,
        display_name=f"Plan {objective_id}",
        assigned_role=f"objectives.{objective_id}.objective-manager",
        base_prompt_metadata=prompt_metadata,
        base_execution_prompt=execution_prompt,
        output_schema_name="objective-outline.response.v1",
        display_schema_name="objective-outline.v1",
        output_prefix=f"{phase}-{objective_id}",
        failure_label=f"objective {objective_id}",
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        timeout_seconds=timeout_seconds,
        planning_limiter=planning_limiter,
        previous_payload=previous_payload,
        validation_error=validation_error,
        normalize_payload=lambda repair_payload: normalize_objective_outline(
            project_root,
            repair_payload,
            run_id=run_id,
            phase=phase,
            objective=objective,
        ),
    )


def load_valid_document(project_root: Path, path: Path, schema_name: str) -> dict[str, Any] | None:
    payload = load_optional_json(path)
    if payload is None:
        return None
    try:
        validate_document(payload, schema_name, project_root)
    except SchemaValidationError:
        return None
    return payload


def load_latest_valid_planning_last_message(
    project_root: Path,
    plans_dir: Path,
    *,
    output_prefix: str,
    schema_name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    repair_paths: list[tuple[int, Path]] = []
    repair_pattern = re.compile(rf"^{re.escape(output_prefix)}\.repair-(?P<attempt>\d+)\.last-message\.json$")
    for path in plans_dir.glob(f"{output_prefix}.repair-*.last-message.json"):
        match = repair_pattern.match(path.name)
        if match is None:
            continue
        repair_paths.append((int(match.group("attempt")), path))
    for _attempt, path in sorted(repair_paths, key=lambda item: item[0], reverse=True):
        payload = load_valid_document(project_root, path, schema_name)
        if payload is not None:
            return payload, "repair"
    base_path = plans_dir / f"{output_prefix}.last-message.json"
    payload = load_valid_document(project_root, base_path, schema_name)
    if payload is not None:
        return payload, "base"
    return None, None


def find_objective(run_dir: Path, objective_id: str) -> dict[str, Any]:
    objective_map = read_json(run_dir / "objective-map.json")
    for objective in objective_map["objectives"]:
        if objective["objective_id"] == objective_id:
            return objective
    raise ValueError(f"Objective {objective_id} was not found")


def objective_uses_single_capability_fast_path(phase: str, objective: dict[str, Any]) -> bool:
    capabilities = [value for value in objective.get("capabilities", []) if isinstance(value, str) and value]
    return phase in {"discovery", "design"} and len(capabilities) == 1


def is_polish_release_repair_context(repair_context: dict[str, Any] | None) -> bool:
    if not isinstance(repair_context, dict):
        return False
    return str(repair_context.get("source") or "").strip() == "polish_release_validation"


def is_user_feedback_repair_context(repair_context: dict[str, Any] | None) -> bool:
    if not isinstance(repair_context, dict):
        return False
    return str(repair_context.get("source") or "").strip() == "user_feedback"


def is_outline_reuse_repair_context(repair_context: dict[str, Any] | None) -> bool:
    return is_polish_release_repair_context(repair_context) or is_user_feedback_repair_context(repair_context)


def should_reuse_existing_outline_for_repair(
    *,
    replace: bool,
    objective: dict[str, Any],
    repair_context: dict[str, Any] | None,
) -> bool:
    if not replace or not is_outline_reuse_repair_context(repair_context):
        return False
    capabilities = [value for value in objective.get("capabilities", []) if isinstance(value, str) and value]
    return len(capabilities) == 1


def should_retry_compact_release_repair(
    exc: BaseException,
    repair_context: dict[str, Any] | None,
    *,
    compact_retry_used: bool,
) -> bool:
    if compact_retry_used or not is_outline_reuse_repair_context(repair_context):
        return False
    message = str(exc)
    return "stalled after" in message or "stall_after_" in message


def compact_release_repair_context(repair_context: dict[str, Any] | None) -> dict[str, Any]:
    compacted = dict(repair_context or {})
    compacted["compact_prompt"] = True
    compacted["compact_retry_used"] = True
    return compacted


def sanitize_outline_for_release_repair(
    project_root: Path,
    run_id: str,
    *,
    objective_id: str,
    outline: dict[str, Any],
    repair_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if is_user_feedback_repair_context(repair_context):
        updated = normalize_outline_run_relative_paths_copy(outline, run_id=run_id)
        existing_file_hints = [
            str(value).strip()
            for value in (repair_context or {}).get("existing_file_hints", [])
            if isinstance(value, str) and str(value).strip()
        ]
        if not existing_file_hints:
            return updated
        owner_capability = str((repair_context or {}).get("owner_capability") or "").strip()
        updated_lanes: list[dict[str, Any]] = []
        for lane in updated.get("capability_lanes", []):
            lane_copy = dict(lane)
            lane_capability = str(lane_copy.get("capability") or "").strip()
            if owner_capability and lane_capability and lane_capability != owner_capability:
                updated_lanes.append(lane_copy)
                continue
            lane_copy["inputs"] = dedupe_strings(list(lane_copy.get("inputs", [])) + existing_file_hints)
            updated_lanes.append(lane_copy)
        updated["capability_lanes"] = updated_lanes
        return updated
    if not is_polish_release_repair_context(repair_context):
        return outline
    phase = str(outline.get("phase") or "polish").strip()
    updated = dict(outline)
    updated_lanes: list[dict[str, Any]] = []
    for lane in updated.get("capability_lanes", []):
        lane_copy = dict(lane)
        capability = str(lane_copy.get("capability") or "").strip()
        release_inputs = build_release_repair_inputs(
            project_root,
            run_id,
            objective_id,
            capability=capability or None,
            phase=phase,
        )
        exact_refs = release_repair_input_refs(release_inputs)
        stable_repo_inputs: list[str] = []
        for value in lane_copy.get("inputs", []):
            if not isinstance(value, str):
                continue
            normalized = value.strip()
            if not normalized or normalized.startswith("runs/"):
                continue
            candidate = project_root / normalized
            if candidate.exists():
                stable_repo_inputs.append(normalized)
        if exact_refs:
            lane_copy["inputs"] = dedupe_strings(stable_repo_inputs + exact_refs)
        else:
            lane_copy["inputs"] = stable_repo_inputs
        updated_lanes.append(lane_copy)
    updated["capability_lanes"] = updated_lanes
    return updated


def normalize_outline_run_relative_paths_copy(payload: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    copied = json.loads(json.dumps(payload))
    normalize_outline_run_relative_paths(copied, run_id=run_id)
    return copied


def default_lane_manager_role(project_root: Path, objective_id: str, capability: str) -> str:
    if capability == "general":
        return f"objectives.{objective_id}.objective-manager"
    objective_root = find_objective_root(project_root, objective_id)
    candidate = objective_root / "approved" / f"{capability}-manager.md"
    if candidate.exists():
        return f"objectives.{objective_id}.{capability}-manager"
    return f"objectives.{objective_id}.objective-manager"


def build_single_capability_fast_path_outline(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    phase: str,
    objective: dict[str, Any],
) -> dict[str, Any]:
    capability = next(
        value for value in objective.get("capabilities", []) if isinstance(value, str) and value
    )
    objective_id = str(objective["objective_id"])
    title = str(objective.get("title") or objective.get("summary") or objective_id).strip()
    summary = str(objective.get("summary") or title or objective_id).strip()
    manager_role = default_lane_manager_role(project_root, objective_id, capability)
    dependency_notes: list[str] = [
        f"Single-capability {phase} objectives skip the separate objective-outline model call and let the {capability} manager decompose the lane directly."
    ]
    objective_map = read_json(run_dir / "objective-map.json")
    inbound_dependencies = [
        dependency
        for dependency in objective_map.get("dependencies", [])
        if dependency.get("to_objective_id") == objective_id
    ]
    for dependency in inbound_dependencies:
        from_objective_id = str(dependency.get("from_objective_id", "")).strip()
        kind = str(dependency.get("kind", "dependency")).strip()
        if from_objective_id:
            dependency_notes.append(
                f"Dependency note: {from_objective_id} remains an external {kind} for this objective and should be consumed through explicit inputs or handoffs rather than extra planning lanes."
            )
    return {
        "schema": "objective-outline.v1",
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "summary": f"Fast-path {phase} plan for {summary}: let the {capability} manager produce the minimal task graph directly.",
        "capability_lanes": [
            {
                "capability": capability,
                "assigned_manager_role": manager_role,
                "objective": f"Own the {phase} work for {title} as one {capability} lane and decompose it into the minimum task graph needed to move the objective forward.",
                "inputs": [
                    "Planning Inputs.goal_context.sections",
                    "Planning Inputs.goal_context.objective_details",
                    "Planning Inputs.prior_phase_reports",
                    "Planning Inputs.related_prior_phase_reports",
                ],
                "expected_outputs": [],
                "done_when": [
                    f"The {capability} manager produced a minimal {phase} task graph for {title} without requiring a separate outline-planning turn."
                ],
                "depends_on": [],
                "planning_notes": [
                    "Single-capability fast path: decompose directly from the objective record and goal context.",
                    "Emit the acceptance review handoff from the final producing task instead of inventing extra planning lanes.",
                ],
                "collaboration_rules": [
                    f"Stay inside the {capability} boundary for this objective.",
                    "Keep acceptance review as an explicit outbound handoff from a producing task.",
                ],
            }
        ],
        "dependency_notes": dependency_notes,
        "collaboration_edges": [],
    }


def previous_phases(phase: str) -> list[str]:
    if phase not in PHASE_SEQUENCE:
        return []
    return PHASE_SEQUENCE[: PHASE_SEQUENCE.index(phase)]


def collect_prior_phase_task_assignments(
    run_dir: Path,
    *,
    objective_id: str,
    before_phase: str,
    capability: str | None = None,
) -> list[dict[str, Any]]:
    phases = set(previous_phases(before_phase))
    tasks: list[dict[str, Any]] = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        payload = read_json(path)
        if payload.get("objective_id") != objective_id or payload.get("phase") not in phases:
            continue
        task_capability = str(payload.get("capability") or "").strip() or None
        if capability is not None and task_capability not in {capability, None}:
            continue
        tasks.append(payload)
    return tasks


def polish_capability_test_root(app_root: Path | None, capability: str) -> Path | None:
    if app_root is None:
        return None
    if capability == "middleware":
        candidate = app_root / "runtime" / "test"
    else:
        candidate = app_root / capability / "test"
    return candidate if candidate.exists() else None


def collect_polish_existing_write_paths(
    project_root: Path,
    run_dir: Path,
    *,
    objective_id: str,
    capability: str,
    phase: str,
) -> list[str]:
    write_paths: list[str] = []
    for task in collect_prior_phase_task_assignments(
        run_dir,
        objective_id=objective_id,
        before_phase=phase,
        capability=capability,
    ):
        candidates = list(task.get("writes_existing_paths", [])) + concrete_expected_output_paths(task)
        for value in candidates:
            normalized = str(value).strip()
            if (
                not normalized
                or normalized.startswith("runs/")
                or "*" in normalized
                or "?" in normalized
                or "[" in normalized
            ):
                continue
            candidate_path = project_root / normalized
            if candidate_path.is_file():
                write_paths.append(normalized)
    return dedupe_strings(write_paths)


def collect_polish_workspace_write_paths(
    project_root: Path,
    *,
    objective_id: str,
    capability: str,
) -> list[str]:
    app_root = find_objective_app_root(project_root, objective_id)
    workspace_root = capability_workspace_root(app_root, capability, phase="polish") if app_root is not None else None
    if workspace_root is None or not workspace_root.exists():
        return []

    allowed_suffixes = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".css", ".html", ".sh"}
    skip_dirs = {"dist", "test", "tests", "coverage", "node_modules", ".git"}
    write_paths: list[str] = []
    resolved_project_root = project_root.resolve()
    for path in sorted(workspace_root.rglob("*")):
        if not path.is_file():
            continue
        relative_parts = path.relative_to(workspace_root).parts
        if relative_parts and relative_parts[0] in skip_dirs:
            continue
        if ".test." in path.name or ".spec." in path.name:
            continue
        if path.suffix and path.suffix not in allowed_suffixes:
            continue
        try:
            write_paths.append(str(path.resolve().relative_to(resolved_project_root)))
        except ValueError:
            continue
    return dedupe_strings(write_paths)


def sanitize_validation_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return normalized or "validation"


def relative_path_for_project(project_root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        try:
            return str(path.relative_to(project_root))
        except ValueError:
            return str(path)


def collect_polish_validation_steps(
    project_root: Path,
    run_dir: Path,
    *,
    run_id: str,
    objective_id: str,
    capability: str,
    phase: str,
) -> list[dict[str, str]]:
    app_root = find_objective_app_root(project_root, objective_id)
    steps: list[dict[str, str]] = []
    seen_commands: set[str] = set()

    def add_step(validation_id: str, command: str) -> None:
        normalized_command = str(command).strip()
        if not normalized_command or normalized_command in seen_commands:
            return
        if classify_polish_validation_command_scope(normalized_command, capability=capability) != "capability":
            return
        seen_commands.add(normalized_command)
        steps.append({"id": sanitize_validation_id(validation_id), "command": normalized_command})

    for task in collect_prior_phase_task_assignments(
        run_dir,
        objective_id=objective_id,
        before_phase=phase,
        capability=capability,
    ):
        for item in task.get("validation", []):
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "").strip()
            if not command:
                continue
            validation_id = str(item.get("id") or f"{capability}-validation").strip()
            add_step(validation_id, command)

    test_root = polish_capability_test_root(app_root, capability)
    if test_root is not None:
        test_files = sorted(
            relative_path_for_project(project_root, path)
            for path in test_root.rglob("*.test.js")
            if path.is_file()
        )
        if test_files:
            add_step(
                f"{capability}-workspace-tests",
                "node --no-warnings --test " + " ".join(test_files),
            )

    return steps


def classify_polish_validation_command_scope(command: str, *, capability: str) -> str:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return "unknown"
    lowered = normalized.lower()
    if " npm test" in f" {lowered}" or lowered.startswith("npm test") or " ci=1 npm test" in f" {lowered}":
        return "phase_gate"
    if "release-readiness" in lowered or "e2e-smoke" in lowered:
        return "phase_gate"

    touched_capabilities: set[str] = set()
    if "apps/todo/frontend/" in lowered:
        touched_capabilities.add("frontend")
    if "apps/todo/backend/" in lowered:
        touched_capabilities.add("backend")
    if "apps/todo/runtime/" in lowered:
        touched_capabilities.add("middleware")
    if len(touched_capabilities) > 1:
        return "phase_gate"
    if touched_capabilities:
        return "capability" if capability in touched_capabilities else "cross_capability"

    script_name = extract_polish_npm_script_name(normalized)
    if not script_name:
        return "capability"
    script_lower = script_name.lower()
    if script_lower == "test" or "release-readiness" in script_lower or "e2e-smoke" in script_lower:
        return "phase_gate"
    if "frontend" in script_lower:
        return "capability" if capability == "frontend" else "cross_capability"
    if "backend" in script_lower:
        return "capability" if capability == "backend" else "cross_capability"
    if "runtime" in script_lower:
        return "capability" if capability == "middleware" else "cross_capability"
    if script_lower == "validate:todo-review-evidence":
        return "capability" if capability == "middleware" else "cross_capability"
    if script_lower == "build":
        return "phase_gate"
    return "capability"


def extract_polish_npm_script_name(command: str) -> str | None:
    normalized = " ".join(str(command or "").strip().split())
    if not normalized:
        return None
    tokens = normalized.split()
    if tokens and tokens[0] == "timeout" and len(tokens) >= 3:
        tokens = tokens[2:]
    while tokens and "=" in tokens[0] and not tokens[0].startswith(("npm", "/")):
        tokens = tokens[1:]
    if len(tokens) >= 3 and tokens[0] == "npm" and tokens[1] == "run":
        return tokens[2]
    if len(tokens) >= 2 and tokens[0] == "npm" and tokens[1] == "test":
        return "test"
    return None


def build_deterministic_polish_outline(
    project_root: Path,
    *,
    run_id: str,
    objective: dict[str, Any],
) -> dict[str, Any]:
    objective_id = str(objective["objective_id"])
    objective_root = find_objective_root(project_root, objective_id)
    objective_root_rel = str(objective_root.relative_to(project_root))
    capabilities = [
        str(value).strip()
        for value in objective.get("capabilities", [])
        if isinstance(value, str) and str(value).strip()
    ] or ["general"]
    lanes: list[dict[str, Any]] = []
    for capability in capabilities:
        summary_output = {
            "kind": "artifact",
            "output_id": f"{capability}-polish-summary",
            "path": f"{objective_root_rel}/polish/{capability}-validation-summary.md",
            "asset_id": None,
            "description": None,
            "evidence": None,
        }
        lanes.append(
            {
                "capability": capability,
                "assigned_manager_role": derive_manager_role_for_capability(project_root, objective_id, capability),
                "objective": f"Polish the existing {capability} implementation, then validate the owned output against the declared polish checklist.",
                "inputs": [
                    "Planning Inputs.prior_phase_reports",
                    "Planning Inputs.prior_phase_artifacts",
                    "Planning Inputs.validation_environment_hints",
                ],
                "expected_outputs": [summary_output],
                "done_when": [
                    f"The {capability} polish summary artifact is produced and the owned validation checklist has been executed."
                ],
                "depends_on": [],
                "planning_notes": [
                    "Polish defaults to deterministic repair-and-validation work instead of fresh decomposition.",
                    "Reuse established artifact paths and existing owned files; do not invent new product structure.",
                ],
                "collaboration_rules": [
                    f"Stay inside the {capability} boundary for this objective.",
                    "Treat polish as bounded hardening and verification, not discovery or redesign.",
                ],
            }
        )
    return {
        "schema": "objective-outline.v1",
        "run_id": run_id,
        "phase": "polish",
        "objective_id": objective_id,
        "summary": f"Deterministic polish outline for {objective_summary_text(objective)}.",
        "capability_lanes": lanes,
        "dependency_notes": [
            "Polish reuses accepted earlier-phase artifacts and validations instead of requesting a new capability decomposition turn."
        ],
        "collaboration_edges": [],
    }


def build_deterministic_polish_objective_plan(
    project_root: Path,
    run_id: str,
    *,
    objective: dict[str, Any],
    outline: dict[str, Any],
    sandbox_mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run_dir = project_root / "runs" / run_id
    objective_id = str(objective["objective_id"])
    objective_root = find_objective_root(project_root, objective_id)
    objective_root_rel = str(objective_root.relative_to(project_root))
    app_root = find_objective_app_root(project_root, objective_id)
    app_root_rel = None
    if app_root is not None:
        try:
            app_root_rel = str(app_root.resolve().relative_to(project_root.resolve()))
        except ValueError:
            try:
                app_root_rel = str(app_root.relative_to(project_root))
            except ValueError:
                app_root_rel = str(app_root)
    capabilities = [
        str(value).strip()
        for value in objective.get("capabilities", [])
        if isinstance(value, str) and str(value).strip()
    ] or ["general"]
    tasks: list[dict[str, Any]] = []
    capability_summaries: list[dict[str, Any]] = []

    for capability in capabilities:
        working_directory = capability_workspace_root(app_root, capability, phase="polish") if app_root is not None else None
        working_directory_rel = app_root_rel
        if isinstance(working_directory, Path) and working_directory.exists():
            try:
                working_directory_rel = str(working_directory.resolve().relative_to(project_root.resolve()))
            except ValueError:
                try:
                    working_directory_rel = str(working_directory.relative_to(project_root))
                except ValueError:
                    working_directory_rel = str(working_directory)
        write_paths = collect_polish_existing_write_paths(
            project_root,
            run_dir,
            objective_id=objective_id,
            capability=capability,
            phase="polish",
        )
        workspace_write_paths = collect_polish_workspace_write_paths(
            project_root,
            objective_id=objective_id,
            capability=capability,
        )
        if workspace_write_paths:
            write_paths = dedupe_strings(write_paths + workspace_write_paths)
        validation_steps = collect_polish_validation_steps(
            project_root,
            run_dir,
            run_id=run_id,
            objective_id=objective_id,
            capability=capability,
            phase="polish",
        )
        implementation_task_id = f"{objective_id}-{capability}-polish-implementation"
        validation_task_id = f"{objective_id}-{capability}-polish-validation"
        implementation_output_path = f"{objective_root_rel}/polish/{capability}-implementation-summary.md"
        validation_output_path = f"{objective_root_rel}/polish/{capability}-validation-summary.md"
        implementation_owned_paths = dedupe_strings(write_paths + [implementation_output_path])
        validation_owned_paths = [validation_output_path]
        implementation_task = {
            "task_id": implementation_task_id,
            "capability": capability,
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": implementation_owned_paths,
            "writes_existing_paths": write_paths,
            "shared_asset_ids": capability_shared_asset_hints(objective_id, capability)[:4],
            "objective": f"Polish the existing {capability} implementation using previously accepted artifacts and keep changes inside the established owned files.",
            "inputs": [
                "Planning Inputs.prior_phase_reports",
                "Planning Inputs.prior_phase_artifacts",
                "Planning Inputs.validation_environment_hints",
            ],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": f"{capability}-polish-implementation-summary",
                    "path": implementation_output_path,
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [
                "Owned implementation files are updated only as needed for polish.",
                "A concise implementation summary artifact is written.",
            ],
            "depends_on": [],
            "validation": [],
            "collaboration_rules": [
                "Reuse established artifact and source paths; do not relocate existing product assets.",
                "Do not broaden scope beyond previously accepted owned files and declared polish outputs.",
            ],
            "working_directory": working_directory_rel,
            "additional_directories": [],
            "sandbox_mode": sandbox_mode,
        }
        validation_task = {
            "task_id": validation_task_id,
            "capability": capability,
            "execution_mode": "isolated_write",
            "parallel_policy": "serialize",
            "owned_paths": validation_owned_paths,
            "writes_existing_paths": [],
            "shared_asset_ids": capability_shared_asset_hints(objective_id, capability)[:4],
            "objective": f"Run the declared {capability} polish validation checklist and summarize the results without redesigning the implementation.",
            "inputs": [
                f"Output of {implementation_task_id}",
                "Planning Inputs.validation_environment_hints",
                "Planning Inputs.prior_phase_reports",
            ],
            "expected_outputs": [
                {
                    "kind": "artifact",
                    "output_id": f"{capability}-polish-summary",
                    "path": validation_output_path,
                    "asset_id": None,
                    "description": None,
                    "evidence": None,
                }
            ],
            "done_when": [
                "The capability polish validation checklist is executed.",
                "A validation summary artifact is written with the latest results.",
            ],
            "depends_on": [implementation_task_id],
            "validation": validation_steps,
            "collaboration_rules": [
                "If a validation is blocked by the environment, report it as an environment blocker rather than requesting product replanning.",
                "Stay within the existing capability boundary and do not invent new cross-capability contracts.",
            ],
            "working_directory": working_directory_rel,
            "additional_directories": [],
            "sandbox_mode": sandbox_mode,
        }
        tasks.extend([implementation_task, validation_task])
        capability_summaries.append(
            {
                "run_id": run_id,
                "phase": "polish",
                "objective_id": objective_id,
                "capability": capability,
                "thread_id": None,
                "usage": None,
                "plan_path": f"runs/{run_id}/manager-plans/polish-{objective_id}-{capability}.json",
                "task_ids": [implementation_task_id, validation_task_id],
                "bundle_ids": [f"{objective_id}-{capability}-polish-bundle"],
                "handoff_ids": [],
                "stdout_path": None,
                "stderr_path": None,
                "last_message_path": None,
                "identity_adjustments": {},
                "attempt": 0,
                "recovery_action": "deterministic_polish_plan",
            }
        )

    plan = {
        "schema": "objective-plan.v1",
        "run_id": run_id,
        "phase": "polish",
        "objective_id": objective_id,
        "summary": f"Deterministic polish plan for {objective_summary_text(objective)}.",
        "tasks": tasks,
        "bundle_plan": [
            {
                "bundle_id": f"{objective_id}-polish-bundle",
                "task_ids": [task["task_id"] for task in tasks],
                "summary": f"Polish bundle for {objective_summary_text(objective)}.",
            }
        ],
        "dependency_notes": [
            "Polish reuses earlier-phase owned files and validations; new decomposition is reserved for fallback repair only."
        ],
        "collaboration_handoffs": [],
    }
    return plan, capability_summaries


def build_planning_prompt(prompt_text: str) -> str:
    return (
        prompt_text
        + "\n\n# Objective Planning Output Requirements\n\n"
        + "Return only one JSON object matching the objective-outline schema.\n"
        + "Do not wrap the JSON in markdown fences.\n"
        + "Copy run_id, phase, and objective_id exactly from the injected Runtime Context and Planning Inputs.\n"
        + "Use only the Runtime Context and Planning Inputs already provided in this prompt.\n"
        + "Use the `# Exact Objective Contract` section as a hard checklist before you return the outline.\n"
        + "Do not inspect the repository, run shell commands, or read additional files.\n"
        + "Do not perform exploratory analysis outside the injected planning inputs.\n"
        + "Return the JSON plan as your first and only response.\n"
        + "Do not execute implementation work.\n"
        + "Keep the objective plan lean: define only the minimum capability lanes and collaboration edges needed to move the current phase forward.\n"
        + "Create capability lanes only for capabilities allowed in the `# Exact Objective Contract` section.\n"
        + "Do not create evidence-only, review-only, or documentation-only lanes when the producing capability can emit those artifacts as part of its own work.\n"
        + "Define capability lanes for the active objective using only roles already present in the injected team definition.\n"
        + "Each capability lane must include objective, inputs, expected_outputs, done_when, depends_on, planning_notes, and collaboration_rules.\n"
        + "Use collaboration_edges only for real cross-lane dependencies that require another capability lane or role.\n"
        + "Every collaboration edge must include edge_id, from_capability, to_capability, to_role, handoff_type, reason, deliverables, blocking, and shared_asset_ids.\n"
        + "For acceptance or review-bundle collaboration edges, include only concrete artifact or asset deliverables. Keep readiness assertions task-local instead of listing them as required handoff deliverables.\n"
        + "For capability lanes that hand off only an acceptance/review bundle, keep lane expected_outputs concrete as well; do not add lane-level readiness assertions.\n"
        + "Use fully qualified objective-scoped role ids for to_role, for example `objectives.<objective_id>.acceptance-manager`.\n"
        + "Every expected_outputs or deliverables entry must be an object with these exact keys: "
        + 'kind, output_id, path, asset_id, description, evidence.\n'
        + 'For artifact outputs: set kind="artifact", fill path, and set asset_id, description, evidence to null.\n'
        + 'For asset outputs: set kind="asset", fill asset_id and path, and set description and evidence to null.\n'
        + 'For assertion outputs: set kind="assertion", fill description and evidence={"validation_ids":[...],"artifact_paths":[...]}, and set path and asset_id to null.\n'
        + "In `mvp-build`, capability-lane expected_outputs and collaboration edge deliverables must use concrete file paths for any artifact or asset output. "
        + "Do not declare whole-app or whole-workspace implementation assets such as `apps/todo`, `apps/todo/frontend`, or `apps/todo/backend`. "
        + "If you need to express overall implementation readiness, use an assertion output plus concrete handoff or review artifact files.\n"
        + "Keep each capability lane's artifact and asset output paths inside the allowed output surfaces listed in the `# Exact Objective Contract` section for that capability.\n"
        + "If Planning Inputs.shared_workspace_ownership lists explicit app-root shared files, only the listed owner capability may emit them. "
        + "Do not claim other shared app-root files just because they sit outside frontend/backend source trees.\n"
        + "For backend `mvp-build`, do not use `frontend-api-consumption-contract.md` as a backend implementation input. "
        + "Use the approved backend design/OpenAPI package plus the middleware reconciled integration contract as the authoritative API inputs.\n"
        + "Shared contract authority is capability-specific. Backend is the only capability that may author shared API contract artifacts or assets. "
        + "Middleware is the only capability that may author shared integration contract artifacts or assets. "
        + "Frontend may author consumer notes or frontend-only handoffs, but must not redefine the shared API or integration contracts.\n"
        + "For task-level assertion outputs, evidence.validation_ids must refer only to validations executed by that same task. "
        + "Do not reference acceptance-manager reviews or downstream handoff checks in a task assertion.\n"
        + "Do not emit plain strings in expected_outputs or deliverables.\n"
    )


def build_capability_planning_prompt(prompt_text: str) -> str:
    return (
        prompt_text
        + "\n\n# Capability Planning Output Requirements\n\n"
        + "Return only one JSON object matching the capability-plan schema.\n"
        + "Do not wrap the JSON in markdown fences.\n"
        + "Copy run_id, phase, objective_id, and capability exactly from the injected Runtime Context and Capability Planning Inputs.\n"
        + "Use only the Runtime Context and Capability Planning Inputs already provided in this prompt.\n"
        + "Use the `# Exact Output Contract` section as a hard checklist before you return the plan.\n"
        + "Do not inspect the repository, run shell commands, or read additional files.\n"
        + "Return the JSON plan as your first and only response.\n"
        + "Do not execute implementation work.\n"
        + "Produce very small isolated worker tasks for this capability lane only.\n"
        + "Each task should cover one tight file cluster, one bounded contract reconciliation, or one validation step that directly follows implementation.\n"
        + "Use only worker roles from the listed objective team when assigning tasks.\n"
        + "Every generated task must include execution_mode, parallel_policy, writes_existing_paths, owned_paths, and shared_asset_ids.\n"
        + "Use execution_mode `read_only` for analysis/reporting work and `isolated_write` for code-writing or file-writing work.\n"
        + "Use parallel_policy `allow` only when you can justify safe isolation from other tasks; otherwise use `serialize`.\n"
        + "Use expected_outputs to declare every new file the task creates.\n"
        + "Use writes_existing_paths only for concrete existing files the task edits.\n"
        + "If a file does not already exist and your task will create it, it belongs in expected_outputs, not writes_existing_paths.\n"
        + "Do not use broad owned_paths as a substitute for write intent; the system derives final owned_paths from created output paths plus writes_existing_paths.\n"
        + "Keep owned_paths empty or aligned exactly to that derived write set. Do not emit repo-wide globs for normal MVP implementation tasks.\n"
        + "In `mvp-build`, every artifact or asset path used for an isolated_write task must be a concrete file path. "
        + "Do not use app roots, capability roots, or source-tree directories such as `apps/todo`, `apps/todo/frontend`, "
        + "or `apps/todo/frontend/src` as expected_outputs paths.\n"
        + "When the capability workspace already exists, keep implementation file outputs and writes_existing_paths inside that capability workspace. "
        + "Only objective-specific artifact files may live under the objective's own orchestration directory.\n"
        + "If Capability Planning Inputs.capability_scope_hints.shared_root_owned_paths is non-empty, those are the only shared app-root files "
        + "this capability may edit or emit in mvp-build. Do not claim other app-root files outside that explicit list.\n"
        + "In `mvp-build`, middleware/integration lanes consume the approved frontend and backend outputs and prove the connection works. "
        + "Do not create a replacement app runtime tree, duplicate frontend bundle, duplicate backend service, or standalone persistence implementation under a new root such as `apps/<app>/runtime`.\n"
        + "Use shared_asset_ids to identify cross-lane contracts, schemas, or shared integration surfaces.\n"
        + "shared_asset_ids are logical identifiers, not outputs by themselves. Do not repeat a shared_asset_id as an asset output unless you also emit a concrete file path for it.\n"
        + "Every task must declare at least one expected_outputs entry.\n"
        + "Use task-level assertion outputs only for read_only reconciliation or analysis tasks that do not produce concrete file outputs.\n"
        + "In `discovery` and `design`, isolated_write producing tasks should declare only the artifact/asset files they create.\n"
        + "Do not add bundle-level assertion outputs or self-check validations over files the same task just wrote.\n"
        + "Every bundle in bundle_plan must reference only generated task ids.\n"
        + "Keep bundle_plan lean: in `mvp-build`, prefer 2-4 tasks for the lane unless there is a concrete ownership boundary that requires more.\n"
        + "In `mvp-build`, implementation comes first. Do not create standalone evidence, report, conformance, review, or handoff tasks when the same artifact can be emitted by the final implementation task or its immediate validation step.\n"
        + "If design and runtime contracts disagree, create one explicit reconciliation task in the producing capability and make downstream consumers depend on it. Do not schedule downstream implementation against contradictory contracts.\n"
        + "In `discovery` and `design`, default to one producing task per lane. If you need more than one task, each task must directly emit at least one capability_lane expected output or required outbound handoff output.\n"
        + "Do not split internal synthesis from later bundle/package/materialization tasks when the producing task can emit the final artifacts itself.\n"
        + "For every phase, each task input must be either a concrete repo-relative file path, "
        + "an explicit `Output of <task-id>` reference, or a dotted `Planning Inputs.`/`Runtime Context.` reference.\n"
        + "Do not write natural-language placeholders such as `Planning input existing_phase_tasks... defining ...`.\n"
        + "When referencing an existing planned task, use the exact keyed path form "
        + "`Planning Inputs.existing_capability_tasks_by_id.<task-id>.<field>` or "
        + "`Planning Inputs.existing_phase_tasks_by_id.<task-id>.<field>`.\n"
        + "When prior-phase reports or artifacts are available in Capability Planning Inputs, prefer referencing those exact paths "
        + "instead of vague English placeholders such as 'approved design package'.\n"
        + "When a task depends on another same-phase task in this capability lane, reference it as `Output of <task-id>`, not the future file path.\n"
        + "When a task depends on a required inbound handoff deliverable, reference it with the exact `Planning Inputs.required_inbound_handoffs[...]` path, not the future file path.\n"
        + "Do not reference future same-phase landed artifacts such as `runs/<run>/artifacts/...`, `runs/<run>/reports/...`, or `runs/<run>/review-bundles/...` as repo inputs; "
        + "those must be expressed as `Output of <task-id>` or `Planning Inputs.required_inbound_handoffs[...]` references instead.\n"
        + "Do not place nonexistent future repo paths from same-phase work into task inputs.\n"
        + "Emit collaboration_handoffs only for real outbound cross-lane handoffs produced by tasks in this capability lane, and tie them to concrete local from_task_id values.\n"
        + "Every collaboration_handoff must include handoff_id, from_capability, to_capability, from_task_id, to_role, handoff_type, reason, deliverable_output_ids, blocking, and shared_asset_ids.\n"
        + "Use fully qualified objective-scoped role ids for collaboration_handoffs.to_role.\n"
        + "Every expected_outputs entry must be an object with these exact keys: "
        + 'kind, output_id, path, asset_id, description, evidence.\n'
        + 'For artifact outputs: set kind="artifact", fill path, and set asset_id, description, evidence to null.\n'
        + 'For asset outputs: set kind="asset", fill asset_id and path, and set description and evidence to null.\n'
        + 'For assertion outputs: set kind="assertion", fill description and evidence={"validation_ids":[...],"artifact_paths":[...]}, and set path and asset_id to null.\n'
        + "For task-level assertion outputs, evidence.validation_ids must refer only to validations executed by that same task. "
        + "Do not reference acceptance-manager reviews or downstream handoff checks in a task assertion.\n"
        + "Do not emit plain strings in expected_outputs.\n"
        + "Every collaboration_handoff.deliverable_output_ids entry must reference an output_id declared by the handoff's from_task_id.\n"
        + "Across the capability plan, the union of task.expected_outputs must cover every output_id declared in Capability Planning Inputs.capability_lane.expected_outputs.\n"
        + "The final lane outputs you cover must match the exact items listed in the `# Exact Output Contract` section.\n"
        + "Do not invent additional final lane outputs beyond that section.\n"
        + "If the lane expected_outputs include test files, validation runners, review bundles, or other terminal artifacts, attach them to the final producing task that creates or packages them. Do not omit required lane outputs from task.expected_outputs.\n"
        + "When a collaboration_handoff materializes a required outbound edge, its deliverable_output_ids must include every output_id required by the matching edge.\n"
        + "Every required outbound handoff listed in the `# Exact Output Contract` section must be covered exactly once by a task-level collaboration_handoff.\n"
        + "Do not claim a deliverable_output_id in a collaboration_handoff unless the handoff source task declares that same output_id in expected_outputs.\n"
        + "Each required outbound handoff edge must be materialized by exactly one collaboration_handoff.\n"
        + "If a required outbound handoff needs outputs from multiple tasks, create one final consolidation task that emits a single review bundle or handoff artifact, then emit the collaboration_handoff from that task.\n"
        + "If Required Outbound Handoffs are provided in Capability Planning Inputs, cover those with concrete task-level collaboration_handoffs.\n"
        + "Use Required Inbound Handoffs only as dependencies or inputs for your tasks; do not repeat inbound handoffs in collaboration_handoffs.\n"
        + "For backend `mvp-build`, do not add `frontend-api-consumption-contract.md` to task inputs. "
        + "Frontend consumer notes are not a peer transport contract for backend implementation; use the approved backend design/OpenAPI inputs and middleware reconciled integration contract instead.\n"
        + "Shared contract authority is capability-specific. Backend is the only capability that may emit shared API contract files or asset ids. "
        + "Middleware is the only capability that may emit shared integration contract files or asset ids. "
        + "Frontend may emit consumer notes for its own lane, but must not redefine shared API or integration contracts.\n"
        + "Every validation.command must be a real shell command that could run in a normal developer environment. Do not invent placeholder executables such as `check-discovery-bundle` or `check-design-package`.\n"
    )


def planning_repair_payload_slice(
    previous_payload: dict[str, Any],
    *,
    validation_error: str,
    repair_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(previous_payload, dict):
        return previous_payload
    schema_name = str(previous_payload.get("schema") or "").strip()
    if schema_name != "capability-plan.v1":
        return strip_planner_managed_fields(previous_payload)
    task_id = extract_task_id_from_planning_error(validation_error)
    if not task_id:
        return strip_planner_managed_fields(previous_payload)
    tasks = list(previous_payload.get("tasks") or [])
    task_by_id = {
        str(task.get("task_id") or "").strip(): task
        for task in tasks
        if isinstance(task, dict) and str(task.get("task_id") or "").strip()
    }
    if task_id not in task_by_id:
        return strip_planner_managed_fields(previous_payload)
    kept_task_ids: set[str] = {task_id}
    queue = [task_id]
    while queue:
        current_task_id = queue.pop(0)
        current_task = task_by_id.get(current_task_id)
        if not isinstance(current_task, dict):
            continue
        for dep in current_task.get("depends_on", []):
            dep_id = str(dep).strip()
            if dep_id and dep_id in task_by_id and dep_id not in kept_task_ids:
                kept_task_ids.add(dep_id)
                queue.append(dep_id)
    sliced = {
        key: previous_payload.get(key)
        for key in (
            "schema",
            "run_id",
            "phase",
            "objective_id",
            "capability",
            "summary",
            "dependency_notes",
        )
        if key in previous_payload
    }
    sliced["tasks"] = [task_by_id[current] for current in kept_task_ids if current in task_by_id]
    sliced["bundle_plan"] = [
        bundle
        for bundle in previous_payload.get("bundle_plan", [])
        if any(str(task_ref).strip() in kept_task_ids for task_ref in bundle.get("task_ids", []))
    ]
    sliced["collaboration_handoffs"] = [
        handoff
        for handoff in previous_payload.get("collaboration_handoffs", [])
        if str(handoff.get("from_task_id") or "").strip() in kept_task_ids
    ]
    if is_polish_release_repair_context(repair_context):
        sliced["repair_scope"] = {
            "focus_paths": list((repair_context or {}).get("focus_paths", [])),
            "source": str((repair_context or {}).get("source") or ""),
        }
    return strip_planner_managed_fields(sliced)


def strip_planner_managed_fields(value: Any, *, top_level: bool = True) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_planner_managed_fields(nested, top_level=False)
            for key, nested in value.items()
            if key not in {"assigned_role", "assigned_manager_role"}
            and not (top_level and key in {"run_id", "phase", "objective_id", "capability"})
        }
    if isinstance(value, list):
        return [strip_planner_managed_fields(item, top_level=False) for item in value]
    return value


def normalize_plan_identity(
    payload: dict[str, Any], *, run_id: str, phase: str, objective_id: str, capability: str | None = None
) -> dict[str, dict[str, str]]:
    adjustments: dict[str, dict[str, str]] = {}
    current_run_id = str(payload.get("run_id") or "").strip()
    current_phase = str(payload.get("phase") or "").strip()
    current_objective_id = str(payload.get("objective_id") or "").strip()
    if current_phase and current_phase != phase:
        raise ExecutorError("Planning output identity does not match the requested objective/phase")
    if current_objective_id and current_objective_id != objective_id:
        raise ExecutorError("Planning output identity does not match the requested objective/phase")
    if current_run_id != run_id:
        adjustments["run_id"] = {"from": current_run_id, "to": run_id}
        payload["run_id"] = run_id
    if current_phase != phase:
        adjustments["phase"] = {"from": current_phase, "to": phase}
        payload["phase"] = phase
    if current_objective_id != objective_id:
        adjustments["objective_id"] = {"from": current_objective_id, "to": objective_id}
        payload["objective_id"] = objective_id
    if capability is not None:
        current_capability = str(payload.get("capability") or "").strip()
        if current_capability and current_capability != capability:
            raise ExecutorError(f"Capability plan identity does not match requested capability {capability}")
        if current_capability != capability:
            adjustments["capability"] = {"from": current_capability, "to": capability}
            payload["capability"] = capability
    return adjustments


def rewrite_run_relative_path(value: str, run_id: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return normalized
    match = re.match(r"^runs/[^/]+/(?P<rest>.+)$", normalized)
    if not match:
        return normalized
    return f"runs/{run_id}/{match.group('rest')}"


def rewrite_run_relative_text(value: str, run_id: str) -> str:
    if not isinstance(value, str) or "runs/" not in value:
        return value
    return re.sub(r"runs/[^/\s'\"`]+/", f"runs/{run_id}/", value)


def normalize_run_relative_output_descriptors(descriptors: list[dict[str, Any]], *, run_id: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for descriptor in descriptors:
        updated = dict(descriptor)
        path = descriptor_path(updated)
        if path:
            updated["path"] = rewrite_run_relative_path(path, run_id)
        normalized.append(updated)
    return normalized


def normalize_outline_run_relative_paths(payload: dict[str, Any], *, run_id: str) -> None:
    for lane in payload.get("capability_lanes", []):
        lane["inputs"] = [
            rewrite_run_relative_path(value, run_id) if isinstance(value, str) else value
            for value in lane.get("inputs", [])
        ]
        lane["expected_outputs"] = normalize_run_relative_output_descriptors(
            normalize_output_descriptors(sanitize_output_descriptors(list(lane.get("expected_outputs", [])))),
            run_id=run_id,
        )
    for edge in payload.get("collaboration_edges", []):
        edge["deliverables"] = normalize_run_relative_output_descriptors(
            normalize_output_descriptors(sanitize_output_descriptors(list(edge.get("deliverables", [])))),
            run_id=run_id,
        )


def normalize_task_run_relative_paths(task: dict[str, Any], *, run_id: str) -> None:
    task["inputs"] = [
        rewrite_run_relative_path(value, run_id) if isinstance(value, str) else value
        for value in task.get("inputs", [])
    ]
    task["owned_paths"] = [
        rewrite_run_relative_path(value, run_id) if isinstance(value, str) else value
        for value in task.get("owned_paths", [])
    ]
    task["writes_existing_paths"] = [
        rewrite_run_relative_path(value, run_id) if isinstance(value, str) else value
        for value in task.get("writes_existing_paths", [])
    ]
    task["additional_directories"] = [
        rewrite_run_relative_path(value, run_id) if isinstance(value, str) else value
        for value in task.get("additional_directories", [])
    ]
    task["expected_outputs"] = normalize_run_relative_output_descriptors(
        normalize_output_descriptors(sanitize_output_descriptors(list(task.get("expected_outputs", [])))),
        run_id=run_id,
    )
    for validation in task.get("validation", []):
        if not isinstance(validation, dict):
            continue
        command = validation.get("command")
        if isinstance(command, str) and command.strip():
            validation["command"] = rewrite_run_relative_text(command, run_id)


def normalize_objective_outline(
    project_root: Path,
    payload: dict[str, Any],
    *,
    run_id: str,
    phase: str,
    objective: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    payload["schema"] = "objective-outline.v1"
    adjustments = normalize_plan_identity(
        payload,
        run_id=run_id,
        phase=phase,
        objective_id=objective["objective_id"],
    )
    expected_capabilities = list(objective.get("capabilities", [])) or ["general"]
    for lane in payload.get("capability_lanes", []):
        normalized_capability = canonical_outline_capability(
            lane.get("capability", ""),
            lane.get("assigned_manager_role", ""),
            expected_capabilities,
        )
        lane["assigned_manager_role"] = derive_manager_role_for_capability(
            project_root,
            objective["objective_id"],
            normalized_capability,
        )
    try:
        for lane in payload.get("capability_lanes", []):
            lane["expected_outputs"] = normalize_output_descriptors(
                sanitize_output_descriptors(list(lane.get("expected_outputs", [])))
            )
        for edge in payload.get("collaboration_edges", []):
            edge["deliverables"] = normalize_output_descriptors(
                sanitize_output_descriptors(list(edge.get("deliverables", [])))
            )
    except ValueError as exc:
        raise ExecutorError(
            "Objective outline declared an invalid output descriptor. Asset outputs must always be backed by "
            "concrete file paths; use assertion outputs for logical claims instead."
        ) from exc
    normalize_outline_run_relative_paths(payload, run_id=run_id)
    try:
        validate_document(payload, "objective-outline.v1", project_root)
    except SchemaValidationError as exc:
        raise ExecutorError(f"Objective manager returned invalid objective outline: {exc}") from exc
    payload["capability_lanes"], lane_aliases = normalize_outline_lanes(
        project_root,
        objective["objective_id"],
        payload["capability_lanes"],
        expected_capabilities,
    )
    seen_edge_ids: set[str] = set()
    normalized_edges: list[dict[str, Any]] = []
    for edge in payload.get("collaboration_edges", []):
        edge_id = edge["edge_id"]
        if not edge_id.startswith(f"{objective['objective_id']}-"):
            edge_id = f"{objective['objective_id']}-{edge_id}"
            edge["edge_id"] = edge_id
        edge["to_role"] = normalize_role_reference(objective["objective_id"], edge["to_role"])
        edge["from_capability"] = lane_aliases.get(edge["from_capability"], edge["from_capability"])
        if not allows_non_lane_target(objective["objective_id"], edge["to_capability"], edge["to_role"]):
            edge["to_capability"] = lane_aliases.get(edge["to_capability"], edge["to_capability"])
        if (
            edge["from_capability"] == edge["to_capability"]
            and edge["from_capability"] in expected_capabilities
            and not allows_non_lane_target(objective["objective_id"], edge["to_capability"], edge["to_role"])
        ):
            continue
        if edge["from_capability"] not in expected_capabilities:
            raise ExecutorError(
                f"Objective outline proposed collaboration edge {edge['edge_id']} for an unexpected capability"
            )
        if edge["to_capability"] not in expected_capabilities and not allows_non_lane_target(
            objective["objective_id"],
            edge["to_capability"],
            edge["to_role"],
        ):
            raise ExecutorError(
                f"Objective outline proposed collaboration edge {edge['edge_id']} for an unexpected capability"
            )
        if edge_id in seen_edge_ids:
            raise ExecutorError(f"Objective outline duplicated collaboration edge {edge_id}")
        seen_edge_ids.add(edge_id)
        edge["shared_asset_ids"] = dedupe_strings(
            [item for item in edge.get("shared_asset_ids", []) if isinstance(item, str) and item] or [edge_id]
        )
        edge["deliverables"] = normalize_output_descriptors(
            sanitize_output_descriptors(list(edge.get("deliverables", [])))
        )
        normalized_edges.append(edge)
    payload["collaboration_edges"] = normalized_edges
    strip_assertion_deliverables_from_review_edges(payload)
    strip_assertion_outputs_from_review_lanes(payload)
    if not payload["capability_lanes"]:
        raise ExecutorError("Objective outline must include at least one capability lane")
    validate_objective_outline_contract_authority(payload)
    validate_objective_outline_write_targets(
        project_root,
        payload,
        phase=phase,
        objective_id=objective["objective_id"],
    )
    validate_objective_outline_prior_artifact_continuity(
        project_root,
        payload,
        run_id=run_id,
        phase=phase,
        objective_id=objective["objective_id"],
    )
    validate_objective_outline_backend_alignment(
        project_root,
        payload,
        run_id=run_id,
        phase=phase,
        objective_id=objective["objective_id"],
    )
    return payload, adjustments


def normalize_outline_lanes(
    project_root: Path,
    objective_id: str,
    lanes: list[dict[str, Any]],
    expected_capabilities: list[str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    merged: dict[str, dict[str, Any]] = {}
    lane_aliases: dict[str, str] = {}
    for lane in lanes:
        original_capability = lane["capability"]
        normalized_capability = canonical_outline_capability(
            original_capability,
            lane.get("assigned_manager_role", ""),
            expected_capabilities,
        )
        lane_aliases[original_capability] = normalized_capability
        expected_manager_role = derive_manager_role_for_capability(project_root, objective_id, normalized_capability)
        if normalized_capability not in merged:
            normalized_lane = dict(lane)
            normalized_lane["capability"] = normalized_capability
            normalized_lane["assigned_manager_role"] = expected_manager_role
            normalized_lane["depends_on"] = list(lane.get("depends_on", []))
            normalized_lane["expected_outputs"] = normalize_output_descriptors(
                sanitize_output_descriptors(list(lane.get("expected_outputs", [])))
            )
            merged[normalized_capability] = normalized_lane
            continue
        target = merged[normalized_capability]
        target["inputs"] = dedupe_strings(list(target.get("inputs", [])) + list(lane.get("inputs", [])))
        target["expected_outputs"] = normalize_output_descriptors(
            sanitize_output_descriptors(
                list(target.get("expected_outputs", [])) + list(lane.get("expected_outputs", []))
            )
        )
        target["done_when"] = dedupe_strings(list(target.get("done_when", [])) + list(lane.get("done_when", [])))
        target["planning_notes"] = dedupe_strings(
            list(target.get("planning_notes", []))
            + [f"Merged objective outline lane {original_capability} into {normalized_capability}."]
            + list(lane.get("planning_notes", []))
        )
        target["collaboration_rules"] = dedupe_strings(
            list(target.get("collaboration_rules", [])) + list(lane.get("collaboration_rules", []))
        )
        target["objective"] = join_outline_objectives(target.get("objective", ""), lane.get("objective", ""))
    for lane in merged.values():
        lane["depends_on"] = dedupe_strings(
            [
                lane_aliases.get(dep, dep)
                for dep in lane.get("depends_on", [])
                if lane_aliases.get(dep, dep) != lane["capability"]
            ]
        )
    return list(merged.values()), lane_aliases


ARTIFACT_CONTINUITY_TOKEN_STOPWORDS = {
    "a",
    "an",
    "and",
    "app",
    "artifact",
    "artifacts",
    "build",
    "created",
    "current",
    "design",
    "discovery",
    "doc",
    "docs",
    "file",
    "files",
    "flow",
    "flows",
    "md",
    "mvp",
    "note",
    "notes",
    "phase",
    "polish",
    "report",
    "runtime",
    "script",
    "spec",
    "summary",
    "task",
    "tmp",
    "txt",
    "updated",
}


def artifact_identity_tokens(path_value: str) -> set[str]:
    stem = Path(str(path_value)).stem.lower()
    return {
        token
        for token in re.split(r"[^a-z0-9]+", stem)
        if token and token not in ARTIFACT_CONTINUITY_TOKEN_STOPWORDS and not token.isdigit()
    }


def collect_same_objective_prior_artifacts(
    project_root: Path,
    run_id: str,
    *,
    objective_id: str,
    capability: str,
    phase: str,
) -> list[dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    reports = collect_prior_phase_reports(run_dir, objective_id, phase)
    artifacts = collect_prior_phase_artifacts(project_root, reports)
    if capability == "general":
        return artifacts
    return [
        artifact
        for artifact in artifacts
        if artifact.get("capability") in {capability, None}
    ]


def validate_prior_artifact_path_continuity(
    project_root: Path,
    run_id: str,
    *,
    objective_id: str,
    capability: str,
    phase: str,
    descriptors: list[dict[str, Any]],
    owner_label: str,
) -> None:
    if phase == "discovery":
        return
    objective_root = find_objective_root(project_root, objective_id)
    objective_root_rel = str(objective_root.relative_to(project_root))
    prior_artifacts = [
        artifact
        for artifact in collect_same_objective_prior_artifacts(
            project_root,
            run_id,
            objective_id=objective_id,
            capability=capability,
            phase=phase,
        )
        if isinstance(artifact.get("path"), str)
        and artifact["path"].startswith(f"{objective_root_rel}/")
    ]
    if not prior_artifacts:
        return
    for descriptor in normalize_output_descriptors(list(descriptors)):
        current_path = descriptor_path(descriptor)
        if not current_path or current_path.startswith(f"{objective_root_rel}/"):
            continue
        current_tokens = artifact_identity_tokens(current_path)
        if len(current_tokens) < 2:
            continue
        for artifact in prior_artifacts:
            prior_path = str(artifact.get("path") or "").strip()
            if not prior_path or prior_path == current_path:
                continue
            overlap = current_tokens & artifact_identity_tokens(prior_path)
            if len(overlap) < 2:
                continue
            source_task_id = str(artifact.get("source_task_id") or "").strip()
            source_label = f" from earlier task {source_task_id}" if source_task_id else ""
            raise ExecutorError(
                f"{owner_label} silently moved the previously accepted artifact `{prior_path}` to new path "
                f"`{current_path}`{source_label}. Reuse the accepted artifact path instead of inventing a new "
                "location unless an approved migration explicitly changes it."
            )


def validate_objective_outline_write_targets(
    project_root: Path,
    outline: dict[str, Any],
    *,
    phase: str,
    objective_id: str,
) -> None:
    if phase != "mvp-build":
        return
    run_id = str(outline.get("run_id", "") or "").strip() or None
    for lane in outline.get("capability_lanes", []):
        capability = str(lane.get("capability", ""))
        if capability not in {"frontend", "backend", "middleware"}:
            continue
        pseudo_task = {
            "run_id": run_id,
            "capability": capability,
            "execution_mode": "isolated_write",
            "expected_outputs": list(lane.get("expected_outputs", [])),
            "writes_existing_paths": [],
            "owned_paths": [],
        }
        enforce_middleware_mvp_build_consumption_only(project_root, objective_id, phase, pseudo_task)
        enforce_capability_workspace_targets(project_root, objective_id, phase, capability, pseudo_task)
    for edge in outline.get("collaboration_edges", []):
        capability = str(edge.get("from_capability", ""))
        if capability not in {"frontend", "backend", "middleware"}:
            continue
        pseudo_task = {
            "run_id": run_id,
            "capability": capability,
            "execution_mode": "isolated_write",
            "expected_outputs": list(edge.get("deliverables", [])),
            "writes_existing_paths": [],
            "owned_paths": [],
        }
        enforce_middleware_mvp_build_consumption_only(project_root, objective_id, phase, pseudo_task)
        enforce_capability_workspace_targets(project_root, objective_id, phase, capability, pseudo_task)


def strip_assertion_deliverables_from_review_edges(outline: dict[str, Any]) -> None:
    for edge in outline.get("collaboration_edges", []):
        handoff_type = str(edge.get("handoff_type", "") or "").strip()
        to_capability = str(edge.get("to_capability", "") or "").strip()
        if handoff_type not in {"review_bundle", "acceptance_review_bundle"} and to_capability != "acceptance":
            continue
        deliverables = list(edge.get("deliverables", []))
        concrete_only = []
        for item in deliverables:
            if isinstance(item, dict) and str(item.get("kind", "") or "").strip() == "assertion":
                continue
            concrete_only.append(item)
        if concrete_only:
            edge["deliverables"] = concrete_only


def strip_assertion_outputs_from_review_lanes(outline: dict[str, Any]) -> None:
    review_capabilities: set[str] = set()
    for edge in outline.get("collaboration_edges", []):
        handoff_type = str(edge.get("handoff_type", "") or "").strip()
        to_capability = str(edge.get("to_capability", "") or "").strip()
        if handoff_type in {"review_bundle", "acceptance_review_bundle"} or to_capability == "acceptance":
            from_capability = str(edge.get("from_capability", "") or "").strip()
            if from_capability:
                review_capabilities.add(from_capability)
    if not review_capabilities:
        return
    for lane in outline.get("capability_lanes", []):
        capability = str(lane.get("capability", "") or "").strip()
        if capability not in review_capabilities:
            continue
        outputs = list(lane.get("expected_outputs", []))
        concrete_only = []
        removed_assertion = False
        for item in outputs:
            if isinstance(item, dict) and str(item.get("kind", "") or "").strip() == "assertion":
                removed_assertion = True
                continue
            concrete_only.append(item)
        if removed_assertion and concrete_only:
            lane["expected_outputs"] = concrete_only


def canonical_outline_capability(capability: str, assigned_manager_role: str, expected_capabilities: list[str]) -> str:
    if capability in expected_capabilities:
        return capability
    role_name = assigned_manager_role.split(".")[-1]
    if role_name.endswith("-manager"):
        role_capability = role_name[: -len("-manager")]
        if role_capability in expected_capabilities:
            return role_capability
    for expected in expected_capabilities:
        if capability == expected or capability.startswith(f"{expected}-") or capability.endswith(f"-{expected}"):
            return expected
    raise ExecutorError(f"Objective outline proposed unexpected capability lane {capability}")


def validate_objective_outline_backend_alignment(
    project_root: Path,
    outline: dict[str, Any],
    *,
    run_id: str,
    phase: str,
    objective_id: str,
) -> None:
    if phase != "mvp-build":
        return
    backend_lanes = [
        lane for lane in outline.get("capability_lanes", []) if lane.get("capability") == "backend"
    ]
    if not backend_lanes:
        return
    conflicting_inputs = sorted(
        {
            normalized
            for lane in backend_lanes
            for value in lane.get("inputs", [])
            if isinstance(value, str)
            for normalized in [value.strip()]
            if normalized and is_frontend_consumption_contract_path(normalized)
        }
    )
    if conflicting_inputs:
        raise ExecutorError(
            "Objective outline for backend mvp-build must not consume frontend-api-consumption-contract.md directly. "
            "Use the approved backend design/OpenAPI package and middleware reconciled integration contract as the "
            "authoritative API inputs instead."
        )
    run_dir = project_root / "runs" / run_id
    related_reports = collect_related_app_prior_phase_reports(project_root, run_dir, objective_id, "backend", phase)
    approved_signals = " ".join(str(report.get("summary", "")) for report in related_reports).lower()
    if "sqlite" not in approved_signals:
        return
    serialized_outline = json.dumps(backend_lanes, sort_keys=True).lower()
    conflicting_signals = [
        "todos.json",
        "json-file",
        "json file",
        "file-backed repository",
        "file-backed persistence",
        "durable json persistence",
        "json persistence",
    ]
    if any(signal in serialized_outline for signal in conflicting_signals):
        raise ExecutorError(
            "Objective outline contradicted the approved backend persistence stack: related prior-phase backend reports "
            "lock the app to SQLite, but the emitted mvp-build backend lane reintroduced JSON-file persistence."
        )


def validate_objective_outline_prior_artifact_continuity(
    project_root: Path,
    outline: dict[str, Any],
    *,
    run_id: str,
    phase: str,
    objective_id: str,
) -> None:
    for lane in outline.get("capability_lanes", []):
        capability = str(lane.get("capability", "") or "").strip()
        if not capability:
            continue
        validate_prior_artifact_path_continuity(
            project_root,
            run_id,
            objective_id=objective_id,
            capability=capability,
            phase=phase,
            descriptors=list(lane.get("expected_outputs", [])),
            owner_label=f"Objective outline lane {capability}",
        )


def validate_contract_authority_for_descriptors(
    descriptors: list[dict[str, Any]],
    *,
    capability: str,
    owner_label: str,
) -> None:
    for descriptor in normalize_output_descriptors(list(descriptors)):
        contract_kind = contract_kind_for_descriptor(descriptor)
        if contract_kind is None:
            continue
        if contract_kind == "consumer":
            if capability != "frontend":
                raise ExecutorError(
                    f"{owner_label} must not emit frontend consumer contract outputs. "
                    "Only the frontend capability may author consumer notes."
                )
            continue
        authoritative_capability = authoritative_capability_for_contract_kind(contract_kind)
        if not capability_may_author_contract(capability, contract_kind):
            raise ExecutorError(
                f"{owner_label} must not emit shared {contract_kind} contract outputs. "
                f"The {authoritative_capability} capability is the only authoritative producer for that contract."
            )


def validate_nonfrontend_consumer_contract_inputs(
    inputs: list[str],
    *,
    capability: str,
    owner_label: str,
) -> None:
    if capability == "frontend":
        return
    conflicting_inputs = sorted(
        {
            normalized
            for value in inputs
            if isinstance(value, str)
            for normalized in [value.strip()]
            if normalized and contract_kind_for_reference(path=normalized) == "consumer"
        }
    )
    if conflicting_inputs:
        raise ExecutorError(
            f"{owner_label} must not consume frontend consumer contract files directly. "
            "Use the canonical backend API contract and middleware integration contract instead."
        )


def validate_objective_outline_contract_authority(outline: dict[str, Any]) -> None:
    for lane in outline.get("capability_lanes", []):
        capability = str(lane.get("capability", "") or "").strip()
        if not capability:
            continue
        validate_contract_authority_for_descriptors(
            list(lane.get("expected_outputs", [])),
            capability=capability,
            owner_label=f"Objective outline lane {capability}",
        )
        validate_nonfrontend_consumer_contract_inputs(
            list(lane.get("inputs", [])),
            capability=capability,
            owner_label=f"Objective outline lane {capability}",
        )
    for edge in outline.get("collaboration_edges", []):
        capability = str(edge.get("from_capability", "") or "").strip()
        if not capability:
            continue
        validate_contract_authority_for_descriptors(
            list(edge.get("deliverables", [])),
            capability=capability,
            owner_label=f"Objective outline collaboration edge {edge.get('edge_id', capability)}",
        )


def join_outline_objectives(existing: str, incoming: str) -> str:
    existing = existing.strip()
    incoming = incoming.strip()
    if not existing:
        return incoming
    if not incoming or incoming == existing:
        return existing
    return f"{existing}\n\nMerged scope:\n- {incoming}"


def allows_non_lane_target(objective_id: str, to_capability: str, to_role: str) -> bool:
    capability_aliases = {
        "objective-management": "objective",
    }
    normalized_capability = capability_aliases.get(to_capability, to_capability)
    allowed = {
        (f"objectives.{objective_id}.acceptance-manager", "acceptance"),
        (f"objectives.{objective_id}.objective-manager", "objective"),
    }
    return (to_role, normalized_capability) in allowed


def normalize_role_reference(objective_id: str, role_ref: str) -> str:
    normalized = str(role_ref or "").strip()
    if not normalized or normalized.startswith("objectives."):
        return normalized
    return f"objectives.{objective_id}.{normalized}"


def accumulate_planning_observability(
    current: dict[str, Any],
    *,
    latency_ms: int,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    stdout_bytes: int = 0,
    stderr_bytes: int = 0,
    timed_out: bool,
    timeout_retry_scheduled: bool,
) -> dict[str, Any]:
    updated = dict(current)
    updated["llm_call_count"] = int(updated.get("llm_call_count", 0)) + 1
    updated["last_call_latency_ms"] = latency_ms
    updated["timeout_count"] = int(updated.get("timeout_count", 0)) + (1 if timed_out else 0)
    updated["timeout_retry_count"] = int(updated.get("timeout_retry_count", 0)) + (
        1 if timeout_retry_scheduled else 0
    )
    updated["input_tokens"] = int(updated.get("input_tokens", 0)) + input_tokens
    updated["cached_input_tokens"] = int(updated.get("cached_input_tokens", 0)) + cached_input_tokens
    updated["output_tokens"] = int(updated.get("output_tokens", 0)) + output_tokens
    updated["stdout_bytes"] = int(updated.get("stdout_bytes", 0)) + stdout_bytes
    updated["stderr_bytes"] = int(updated.get("stderr_bytes", 0)) + stderr_bytes
    return updated


def normalize_capability_plan(
    project_root: Path,
    payload: dict[str, Any],
    *,
    run_id: str,
    phase: str,
    objective_id: str,
    capability: str,
    objective_outline: dict[str, Any],
    default_sandbox_mode: str,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    payload["schema"] = "capability-plan.v1"
    adjustments = normalize_plan_identity(
        payload,
        run_id=run_id,
        phase=phase,
        objective_id=objective_id,
        capability=capability,
    )
    for task in payload.get("tasks", []):
        normalize_task_run_relative_paths(task, run_id=run_id)
    backfill_terminal_lane_outputs(payload, objective_outline=objective_outline, capability=capability, phase=phase)
    normalize_task_execution_metadata(
        project_root,
        objective_id,
        capability,
        payload,
        run_id=run_id,
        default_sandbox_mode=default_sandbox_mode,
    )
    normalize_task_input_references(payload)
    canonicalize_same_phase_input_refs(project_root, payload, objective_outline=objective_outline, capability=capability)
    reject_noncanonical_future_path_inputs(
        project_root,
        payload,
        run_id=run_id,
        objective_outline=objective_outline,
        capability=capability,
    )
    attach_required_inbound_handoff_assets(payload, objective_outline=objective_outline, capability=capability)
    normalize_bundle_ids(payload)
    normalize_collaboration_handoffs(payload, objective_id=objective_id, capability=capability)
    align_required_outbound_handoff_output_ids(payload, objective_outline=objective_outline, capability=capability)
    try:
        validate_document(payload, "capability-plan.v1", project_root)
    except SchemaValidationError as exc:
        raise ExecutorError(f"Capability manager returned invalid capability plan: {exc}") from exc
    validate_capability_plan_contents(
        project_root,
        payload,
        run_id=run_id,
        phase=phase,
        objective_id=objective_id,
        capability=capability,
        objective_outline=objective_outline,
    )
    return payload, adjustments


def aggregate_capability_plans(
    project_root: Path,
    run_id: str,
    phase: str,
    objective_id: str,
    outline: dict[str, Any],
    capability_plans: list[dict[str, Any]],
) -> dict[str, Any]:
    task_ids: set[str] = set()
    bundle_ids: set[str] = set()
    tasks: list[dict[str, Any]] = []
    bundle_plan: list[dict[str, Any]] = []
    dependency_notes = list(outline.get("dependency_notes", []))
    capability_handoffs: list[dict[str, Any]] = []

    for plan in capability_plans:
        for task in plan["tasks"]:
            if task["task_id"] in task_ids:
                raise ExecutorError(f"Capability plans duplicated task id {task['task_id']}")
            task_ids.add(task["task_id"])
            tasks.append(task)
        for bundle in plan["bundle_plan"]:
            if bundle["bundle_id"] in bundle_ids:
                raise ExecutorError(f"Capability plans duplicated bundle id {bundle['bundle_id']}")
            bundle_ids.add(bundle["bundle_id"])
            bundle_plan.append(bundle)
        dependency_notes.extend(plan.get("dependency_notes", []))
        capability_handoffs.extend(plan.get("collaboration_handoffs", []))

    collaboration_handoffs = materialize_capability_handoffs(tasks, capability_handoffs)

    normalize_task_dependencies(tasks)
    validate_required_handoffs(outline, collaboration_handoffs)
    attach_handoff_shared_assets(tasks, collaboration_handoffs)
    attach_handoff_dependencies(tasks, collaboration_handoffs)

    plan = {
        "schema": "objective-plan.v1",
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "summary": outline["summary"],
        "tasks": tasks,
        "bundle_plan": bundle_plan,
        "dependency_notes": dedupe_strings(dependency_notes),
        "collaboration_handoffs": dedupe_dicts(collaboration_handoffs),
    }
    canonicalize_planned_task_worker_roles(
        plan,
        objective_id=objective_id,
        default_capability=None,
    )
    try:
        validate_document(plan, "objective-plan.v1", project_root)
    except SchemaValidationError as exc:
        raise ExecutorError(f"Aggregated capability plan was invalid: {exc}") from exc
    normalize_bundle_ids(plan)
    return plan


def normalize_task_dependencies(tasks: list[dict[str, Any]]) -> None:
    valid_task_ids = {task["task_id"] for task in tasks}
    for task in tasks:
        dependencies = [value for value in task.get("depends_on", []) if isinstance(value, str)]
        task["depends_on"] = [value for value in dedupe_strings(dependencies) if value in valid_task_ids]


def normalize_bundle_ids(payload: dict[str, Any]) -> None:
    objective_id = payload["objective_id"]
    seen: set[str] = set()
    for bundle in payload.get("bundle_plan", []):
        bundle_id = bundle["bundle_id"]
        summary = str(bundle.get("summary", "")).strip()
        if not summary:
            review_prompt = str(bundle.get("review_prompt", "")).strip()
            bundle["summary"] = review_prompt or f"Bundle for {bundle_id}"
            summary = str(bundle["summary"]).strip()
        if not bundle_id.startswith(f"{objective_id}-"):
            bundle_id = f"{objective_id}-{bundle_id}"
            bundle["bundle_id"] = bundle_id
        task_ids = [task_id for task_id in bundle.get("task_ids", []) if isinstance(task_id, str)]
        bundle.clear()
        bundle.update(
            {
                "bundle_id": bundle_id,
                "task_ids": dedupe_strings(task_ids),
                "summary": summary or f"Bundle for {bundle_id}",
            }
        )
        if bundle_id in seen:
            raise ExecutorError(f"Planning output duplicated bundle id {bundle_id}")
        seen.add(bundle_id)


def normalize_task_execution_metadata(
    project_root: Path,
    objective_id: str,
    capability: str,
    payload: dict[str, Any],
    *,
    run_id: str | None = None,
    default_sandbox_mode: str,
) -> None:
    canonicalize_planned_task_worker_roles(
        payload,
        objective_id=objective_id,
        default_capability=capability,
    )
    phase = str(payload.get("phase", "discovery"))
    for task in payload.get("tasks", []):
        task["expected_outputs"] = normalize_output_descriptors(
            sanitize_output_descriptors(list(task.get("expected_outputs", [])))
        )
    planned_output_paths: set[str] = {
        path
        for task in payload.get("tasks", [])
        for path in concrete_expected_output_paths(task)
    }
    for task in payload.get("tasks", []):
        normalize_task_execution_entry(
            project_root,
            objective_id,
            capability,
            task,
            phase_override=phase,
            run_id=run_id,
            default_sandbox_mode=default_sandbox_mode,
            available_existing_paths=planned_output_paths,
        )
        planned_output_paths.update(concrete_expected_output_paths(task))


def canonical_worker_role_for_task(
    *,
    objective_id: str,
    task_capability: str | None,
    default_capability: str | None,
) -> str:
    normalized_capability = str(task_capability or default_capability or "").strip()
    if not normalized_capability or normalized_capability == "general":
        return f"objectives.{objective_id}.general-worker"
    return f"objectives.{objective_id}.{normalized_capability}-worker"


def canonicalize_planned_task_worker_roles(
    payload: dict[str, Any],
    *,
    objective_id: str,
    default_capability: str | None,
) -> None:
    for task in payload.get("tasks", []):
        task["assigned_role"] = canonical_worker_role_for_task(
            objective_id=objective_id,
            task_capability=str(task.get("capability", "") or "").strip() or None,
            default_capability=default_capability,
        )


def normalize_task_execution_entry(
    project_root: Path,
    objective_id: str,
    capability: str,
    task: dict[str, Any],
    *,
    phase_override: str | None = None,
    run_id: str | None = None,
    default_sandbox_mode: str,
    available_existing_paths: set[str] | None = None,
) -> None:
    task["capability"] = str(task.get("capability") or capability).strip() or capability
    normalize_task_artifact_descriptors(task)
    task["expected_outputs"] = normalize_output_descriptors(
        sanitize_output_descriptors(list(task.get("expected_outputs", [])))
    )
    phase = str(phase_override or task.get("phase", "discovery"))
    owned_path_hints = capability_owned_path_hints(
        project_root,
        objective_id,
        capability,
        phase=phase,
    )
    shared_asset_hints = capability_shared_asset_hints(objective_id, capability)
    legacy_owned = [item for item in task.get("owned_paths", []) if isinstance(item, str) and item]
    existing_write_paths = normalize_existing_write_paths(
        project_root,
        task.get("writes_existing_paths", []),
        available_existing_paths=available_existing_paths,
        search_roots=existing_write_search_roots(project_root, run_id),
    )
    inferred_existing_write_paths = infer_existing_write_paths_from_expected_outputs(
        project_root,
        task,
        run_id=run_id,
    )
    if inferred_existing_write_paths:
        existing_write_paths = dedupe_strings(existing_write_paths + inferred_existing_write_paths)
    task["writes_existing_paths"] = existing_write_paths
    explicit_parallel_policy = task.get("parallel_policy") is not None
    inferred = infer_execution_metadata(
        phase=phase,
        task_id=str(task.get("task_id", "")),
        expected_outputs=task.get("expected_outputs", []),
        writes_existing_paths=existing_write_paths,
        existing=task,
    )
    task.update(inferred)
    concrete_output_paths = concrete_expected_output_paths(task)
    derived_owned = dedupe_strings(concrete_output_paths + existing_write_paths)
    if task["execution_mode"] == "read_only" and (legacy_owned or derived_owned):
        task["execution_mode"] = "isolated_write"
        if not explicit_parallel_policy:
            task["parallel_policy"] = "serialize"
    if task["execution_mode"] == "isolated_write":
        if derived_owned:
            task["owned_paths"] = derived_owned
        else:
            current_owned = legacy_owned
            if concrete_output_paths:
                current_owned.extend(concrete_output_paths)
            if not current_owned:
                current_owned.extend(owned_path_hints)
            task["owned_paths"] = normalize_owned_paths(
                project_root,
                current_owned=current_owned,
                concrete_output_paths=concrete_output_paths,
                fallback_hints=owned_path_hints,
            )
    else:
        task["owned_paths"] = []
    enforce_explicit_write_targets(project_root, phase, capability, task, run_id=run_id)
    enforce_middleware_mvp_build_consumption_only(project_root, objective_id, phase, task)
    enforce_capability_workspace_targets(project_root, objective_id, phase, capability, task, run_id=run_id)
    enforce_capability_owned_path_bounds(project_root, objective_id, capability, task["owned_paths"])
    current_shared_assets = [item for item in task.get("shared_asset_ids", []) if isinstance(item, str) and item]
    if not current_shared_assets and task_mentions_shared_surface(task):
        current_shared_assets.extend(shared_asset_hints)
    task["shared_asset_ids"] = dedupe_strings(current_shared_assets)
    canonicalize_validation_commands(task)
    normalize_invalid_prefix_validation_commands(
        project_root,
        objective_id=objective_id,
        capability=capability,
        phase=phase,
        task=task,
    )
    prune_discovery_design_producing_task_contract(task, phase=phase)
    task["working_directory"] = None
    task["sandbox_mode"] = effective_sandbox_mode(task, default_sandbox_mode)
    task.setdefault("additional_directories", [])


def backfill_terminal_lane_outputs(
    payload: dict[str, Any],
    *,
    objective_outline: dict[str, Any],
    capability: str,
    phase: str,
) -> None:
    if phase != "mvp-build":
        return
    lane = next(
        (item for item in objective_outline.get("capability_lanes", []) if item.get("capability") == capability),
        None,
    )
    if lane is None:
        return
    required_outputs = normalize_output_descriptors(list(lane.get("expected_outputs", [])))
    if not required_outputs:
        return
    tasks = payload.get("tasks", [])
    if not tasks:
        return
    produced_output_ids = {
        descriptor_output_id(item)
        for task in tasks
        for item in normalize_output_descriptors(list(task.get("expected_outputs", [])))
    }
    missing_outputs = [
        descriptor
        for descriptor in required_outputs
        if descriptor_output_id(descriptor) not in produced_output_ids and descriptor_kind(descriptor) != "assertion"
    ]
    if not missing_outputs:
        return
    downstream_task_ids = {
        str(dep).strip()
        for task in tasks
        for dep in task.get("depends_on", [])
        if isinstance(dep, str) and str(dep).strip()
    }
    terminal_tasks = [
        task
        for task in tasks
        if str(task.get("task_id", "") or "").strip()
        and str(task.get("task_id", "") or "").strip() not in downstream_task_ids
    ]
    if len(terminal_tasks) != 1:
        return
    terminal_task = terminal_tasks[0]
    if terminal_task.get("execution_mode") != "isolated_write":
        return
    existing_outputs = normalize_output_descriptors(list(terminal_task.get("expected_outputs", [])))
    existing_output_ids = {descriptor_output_id(item) for item in existing_outputs}
    terminal_task["expected_outputs"] = existing_outputs + [
        descriptor for descriptor in missing_outputs if descriptor_output_id(descriptor) not in existing_output_ids
    ]


def normalize_owned_paths(
    project_root: Path,
    *,
    current_owned: list[str],
    concrete_output_paths: list[str],
    fallback_hints: list[str],
) -> list[str]:
    owned = dedupe_strings([value for value in current_owned if isinstance(value, str) and value])
    if concrete_output_paths:
        owned = [
            value
            for value in owned
            if not owned_path_is_broad_superset_of_concrete_outputs(value, concrete_output_paths)
        ]
    repaired = [
        value
        for value in owned
        if owned_path_should_be_retained(project_root, value, concrete_output_paths)
    ]
    if owned_paths_include_real_workspace_target(project_root, repaired, concrete_output_paths):
        return repaired
    repaired.extend(fallback_hints)
    return dedupe_strings(repaired)


def normalize_existing_write_paths(
    project_root: Path,
    values: list[Any] | None,
    *,
    available_existing_paths: set[str] | None = None,
    search_roots: list[Path] | None = None,
) -> list[str]:
    normalized: list[str] = []
    missing_paths: list[str] = []
    planned_paths = available_existing_paths or set()
    roots = search_roots or [project_root]
    for value in values or []:
        if not isinstance(value, str):
            continue
        path_value = value.strip().lstrip("./")
        if not path_value:
            continue
        if any(character in path_value for character in "*?["):
            raise ExecutorError(
                f"writes_existing_paths must use concrete existing file paths, not globs: {path_value}"
            )
        found_existing_file = False
        for root in roots:
            candidate = root / path_value
            if not candidate.exists():
                continue
            if candidate.is_dir():
                raise ExecutorError(
                    f"writes_existing_paths must reference files, not directories: {path_value}"
                )
            found_existing_file = True
            break
        if not found_existing_file and path_value not in planned_paths:
            missing_paths.append(path_value)
            continue
        normalized.append(path_value)
    if missing_paths:
        joined = ", ".join(sorted(dedupe_strings(missing_paths)))
        raise ExecutorError(f"writes_existing_paths referenced missing files: {joined}")
    return dedupe_strings(normalized)


def existing_required_output_paths(
    project_root: Path,
    task: dict[str, Any],
    *,
    run_id: str | None = None,
) -> list[str]:
    existing_paths: list[str] = []
    roots = existing_write_search_roots(project_root, run_id)
    for descriptor in normalize_output_descriptors(list(task.get("expected_outputs", []))):
        if descriptor_kind(descriptor) not in {"artifact", "asset"}:
            continue
        path_value = descriptor_path(descriptor)
        if not path_value:
            continue
        for root in roots:
            candidate = root / path_value
            if not candidate.exists():
                continue
            if candidate.is_dir():
                raise ExecutorError(f"expected_outputs must reference files, not directories: {path_value}")
            existing_paths.append(path_value)
            break
    return dedupe_strings(existing_paths)


def expand_existing_output_target_files(
    project_root: Path,
    path_value: str,
    *,
    run_id: str | None = None,
) -> list[str]:
    normalized = normalize_repo_relative_path(path_value)
    if not normalized:
        return []
    matches: list[str] = []
    for root in existing_write_search_roots(project_root, run_id):
        if any(token in normalized for token in "*?["):
            try:
                candidates = list(root.glob(normalized))
            except Exception:
                candidates = []
        else:
            candidate = root / normalized
            candidates = [candidate] if candidate.exists() else []
        for candidate in candidates:
            if not candidate.exists():
                continue
            if candidate.is_file():
                try:
                    matches.append(str(candidate.relative_to(root)).replace("\\", "/"))
                except ValueError:
                    continue
                continue
            if candidate.is_dir():
                for file_path in candidate.rglob("*"):
                    if not file_path.is_file():
                        continue
                    try:
                        matches.append(str(file_path.relative_to(root)).replace("\\", "/"))
                    except ValueError:
                        continue
    return dedupe_strings(matches)


def infer_existing_write_paths_from_expected_outputs(
    project_root: Path,
    task: dict[str, Any],
    *,
    run_id: str | None = None,
) -> list[str]:
    inferred: list[str] = []
    for descriptor in normalize_output_descriptors(list(task.get("expected_outputs", []))):
        if descriptor_kind(descriptor) not in {"artifact", "asset"}:
            continue
        path_value = descriptor_path(descriptor)
        if not path_value:
            continue
        inferred.extend(expand_existing_output_target_files(project_root, path_value, run_id=run_id))
    return dedupe_strings(inferred)


def existing_write_search_roots(project_root: Path, run_id: str | None) -> list[Path]:
    roots: list[Path] = [project_root]
    if run_id:
        integration_workspace = integration_workspace_path(project_root, run_id)
        if integration_workspace.exists():
            roots.append(integration_workspace)
    unique_roots: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique_roots.append(resolved)
    return unique_roots


def select_capability_validation_script(
    project_root: Path,
    *,
    objective_id: str,
    capability: str,
    phase: str,
) -> str | None:
    app_root = find_objective_app_root(project_root, objective_id)
    hints = build_validation_environment_hints(
        project_root,
        app_root,
        capability=capability,
        phase=phase,
    )
    for command in hints.get("recommended_repo_scripts", []):
        if isinstance(command, str) and command.strip().startswith("npm run validate:"):
            return command.strip()
    for command in hints.get("recommended_repo_scripts", []):
        if isinstance(command, str) and command.strip():
            return command.strip()
    return None


def normalize_shell_command(command: str) -> str:
    try:
        return " ".join(shlex.split(str(command or "").strip(), posix=True))
    except ValueError:
        return " ".join(str(command or "").strip().split())


def validation_environment_hints_for_capability(
    project_root: Path,
    *,
    objective_id: str,
    capability: str,
    phase: str,
) -> dict[str, Any]:
    app_root = find_objective_app_root(project_root, objective_id)
    return build_validation_environment_hints(
        project_root,
        app_root,
        capability=capability,
        phase=phase,
    )


def validation_command_candidate_repo_paths(command: str) -> list[str]:
    prefix_path = validation_command_prefix_path(command)
    candidate_paths: list[str] = []
    for path in validation_command_repo_paths(command):
        normalized = str(path).strip().lstrip("./")
        if not normalized:
            continue
        candidate_paths.append(normalized)
        if prefix_path and normalized and not normalized.startswith(f"{prefix_path.rstrip('/')}/"):
            if normalized.startswith("apps/"):
                continue
            candidate_paths.append(f"{prefix_path.rstrip('/')}/{normalized}")
    return dedupe_strings(candidate_paths)


def validation_template_variants(template: str, *, test_path: str) -> set[str]:
    resolved = template.replace("{test_path}", test_path)
    variants = {normalize_shell_command(resolved)}
    if resolved.startswith("CI=1 "):
        variants.add(normalize_shell_command(resolved.removeprefix("CI=1 ")))
    return variants


def canonical_validation_command_from_catalog(
    command: str,
    *,
    hints: dict[str, Any],
    allow_rewrite: bool = True,
) -> str | None:
    normalized_command = normalize_shell_command(command)
    if not normalized_command:
        return None
    for item in list(hints.get("allowed_validation_commands") or []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind == "exact_command":
            exact = str(item.get("command") or "").strip()
            if exact and normalize_shell_command(exact) == normalized_command:
                return exact
            continue
        if kind != "single_test_template":
            continue
        template = str(item.get("command_template") or "").strip()
        prefixes = [
            str(value).strip().rstrip("/")
            for value in list(item.get("allowed_test_path_prefixes") or [])
            if isinstance(value, str) and str(value).strip().rstrip("/")
        ]
        if not template or not prefixes:
            continue
        matching_paths = [
            path
            for path in validation_command_candidate_repo_paths(command)
            if path.endswith((".test.js", ".spec.js"))
            if any(owned_path_targets_prefix(path, prefix) for prefix in prefixes)
        ]
        if len(matching_paths) != 1:
            continue
        test_path = matching_paths[0]
        if normalized_command in validation_template_variants(template, test_path=test_path):
            return template.replace("{test_path}", test_path)
        if not allow_rewrite:
            continue
        program = validation_command_program(command) or ""
        if program in {"npm", "node"}:
            return template.replace("{test_path}", test_path)
    return None


def validation_command_catalog_issue(command: str, *, hints: dict[str, Any]) -> str | None:
    if canonical_validation_command_from_catalog(command, hints=hints, allow_rewrite=False) is not None:
        return None
    if hints.get("allowed_validation_commands"):
        return "does not match any allowed validation command or single-test template for this capability"
    return None


def normalize_invalid_prefix_validation_commands(
    project_root: Path,
    *,
    objective_id: str,
    capability: str,
    phase: str,
    task: dict[str, Any],
) -> None:
    replacement_command = select_capability_validation_script(
        project_root,
        objective_id=objective_id,
        capability=capability,
        phase=phase,
    )
    hints = validation_environment_hints_for_capability(
        project_root,
        objective_id=objective_id,
        capability=capability,
        phase=phase,
    )
    if not replacement_command:
        replacement_command = None
    for validation in task.get("validation", []):
        command = validation.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        canonical_command = canonical_validation_command_from_catalog(command, hints=hints, allow_rewrite=True)
        if canonical_command and canonical_command != command.strip():
            validation["command"] = canonical_command
            continue
        if validation_command_prefix_issue(project_root, command) is None:
            continue
        stripped = command.strip()
        if stripped.startswith("npm --prefix ") and stripped.endswith(" test") and replacement_command:
            validation["command"] = replacement_command


def enforce_explicit_write_targets(
    project_root: Path,
    phase: str,
    capability: str,
    task: dict[str, Any],
    *,
    run_id: str | None = None,
) -> None:
    if phase != "mvp-build" or task.get("execution_mode") != "isolated_write":
        return
    if capability not in {"frontend", "backend", "middleware"}:
        return
    output_paths = []
    for descriptor in normalize_output_descriptors(list(task.get("expected_outputs", []))):
        if descriptor_kind(descriptor) not in {"artifact", "asset"}:
            continue
        path = descriptor_path(descriptor)
        if path:
            output_paths.append(path)
    invalid_output_paths = [
        path for path in output_paths if not looks_like_concrete_file_target(project_root, path)
    ]
    if invalid_output_paths:
        joined = ", ".join(sorted(invalid_output_paths))
        raise ExecutorError(
            "mvp-build isolated_write tasks must declare concrete file outputs, not directory roots. "
            f"Invalid expected_outputs paths: {joined}"
        )
    if not output_paths and not task.get("writes_existing_paths", []):
        raise ExecutorError(
            "mvp-build isolated_write tasks must declare at least one concrete created file in expected_outputs "
            "or at least one concrete existing file in writes_existing_paths."
        )
    existing_output_paths = existing_required_output_paths(project_root, task, run_id=run_id)
    missing_existing_write_paths = [
        path
        for path in existing_output_paths
        if path not in set(task.get("writes_existing_paths", []))
    ]
    if missing_existing_write_paths:
        joined = ", ".join(sorted(missing_existing_write_paths))
        raise ExecutorError(
            "mvp-build isolated_write tasks must list already-existing required output files in "
            "writes_existing_paths as well as expected_outputs. "
            f"Add these paths to writes_existing_paths: {joined}"
        )


def enforce_capability_workspace_targets(
    project_root: Path,
    objective_id: str,
    phase: str,
    capability: str,
    task: dict[str, Any],
    *,
    run_id: str | None = None,
) -> None:
    if phase != "mvp-build" or task.get("execution_mode") != "isolated_write":
        return
    if capability not in {"frontend", "backend", "middleware"}:
        return
    allowed_prefixes = []
    objective_root = find_objective_root(project_root, objective_id, create=True)
    try:
        allowed_prefixes.append(str(objective_root.resolve().relative_to(project_root.resolve())))
    except ValueError:
        pass
    effective_run_id = str(run_id or task.get("run_id", "") or "").strip()
    if effective_run_id:
        allowed_prefixes.extend(
            [
                f"runs/{effective_run_id}/artifacts",
                f"runs/{effective_run_id}/reports",
                f"runs/{effective_run_id}/review-bundles",
            ]
        )
    app_root = find_objective_app_root(project_root, objective_id)
    if capability != "middleware":
        if app_root is None:
            return
        workspace_root = capability_workspace_root(app_root, capability, phase=task.get("phase"))
        if workspace_root is None:
            return
        try:
            allowed_prefixes.insert(0, str(workspace_root.resolve().relative_to(project_root.resolve())))
        except ValueError:
            return
    elif not allowed_prefixes:
        return
    if app_root is not None:
        allowed_prefixes.extend(capability_owned_shared_workspace_paths(project_root, app_root, capability))
    output_paths = []
    for descriptor in normalize_output_descriptors(list(task.get("expected_outputs", []))):
        if descriptor_kind(descriptor) not in {"artifact", "asset"}:
            continue
        path = descriptor_path(descriptor)
        if path:
            output_paths.append(path)
    invalid_paths = [
        path
        for path in dedupe_strings(output_paths + list(task.get("writes_existing_paths", [])))
        if not any(owned_path_targets_prefix(path, prefix) for prefix in allowed_prefixes)
    ]
    if invalid_paths:
        joined = ", ".join(sorted(invalid_paths))
        allowed = ", ".join(allowed_prefixes)
        if capability == "middleware":
            raise ExecutorError(
                "mvp-build middleware/integration tasks must consume existing frontend/backend outputs and emit only "
                "integration-owned artifacts, explicitly owned shared app-root files, or review assets. Do not create "
                "a parallel app runtime tree. "
                f"Invalid paths: {joined}. Allowed roots: {allowed}"
            )
        raise ExecutorError(
            f"{capability} mvp-build tasks must keep implementation paths inside the capability workspace or objective artifact root. "
            f"Invalid paths: {joined}. Allowed roots: {allowed}"
        )


def enforce_middleware_mvp_build_consumption_only(
    project_root: Path,
    objective_id: str,
    phase: str,
    task: dict[str, Any],
) -> None:
    if phase != "mvp-build" or task.get("capability") != "middleware":
        return
    app_root = find_objective_app_root(project_root, objective_id)
    if app_root is None:
        return
    app_name = app_root.name
    disallowed_prefixes = [
        f"apps/{app_name}/frontend",
        f"apps/{app_name}/backend",
        f"apps/{app_name}/runtime",
    ]
    output_paths = []
    for descriptor in normalize_output_descriptors(list(task.get("expected_outputs", []))):
        if descriptor_kind(descriptor) not in {"artifact", "asset"}:
            continue
        path = descriptor_path(descriptor)
        if path:
            output_paths.append(path)
    violating = [
        path
        for path in dedupe_strings(list(task.get("owned_paths", [])) + list(task.get("writes_existing_paths", [])) + output_paths)
        if any(owned_path_targets_prefix(path, prefix) for prefix in disallowed_prefixes)
    ]
    if violating:
        joined = ", ".join(sorted(violating))
        raise ExecutorError(
            "mvp-build middleware/integration tasks must consume existing frontend/backend outputs and emit only "
            "integration-owned artifacts or review assets. Do not create a parallel app runtime tree. "
            f"Invalid paths: {joined}"
        )


def looks_like_concrete_file_target(project_root: Path, path_value: str) -> bool:
    normalized = str(path_value or "").strip().lstrip("./")
    if not normalized or any(character in normalized for character in "*?["):
        return False
    candidate = project_root / normalized
    if candidate.exists():
        return candidate.is_file()
    basename = Path(normalized).name
    if basename in {"Dockerfile", "Makefile", "Procfile"}:
        return True
    return "." in basename


def prune_discovery_design_producing_task_contract(task: dict[str, Any], *, phase: str) -> None:
    if phase not in {"discovery", "design"}:
        return
    if task.get("execution_mode") != "isolated_write":
        return
    outputs = normalize_output_descriptors(list(task.get("expected_outputs", [])))
    file_outputs = [
        item
        for item in outputs
        if descriptor_kind(item) in {"artifact", "asset"} and descriptor_path(item)
    ]
    if not file_outputs:
        return
    if len(file_outputs) != len(outputs):
        task["expected_outputs"] = file_outputs
    output_paths = {
        str(descriptor_path(item)).strip().lstrip("./")
        for item in file_outputs
        if descriptor_path(item)
    }
    filtered_validation = []
    for validation in task.get("validation", []):
        command = validation.get("command")
        if isinstance(command, str) and discovery_design_validation_is_redundant_self_check(
            command,
            output_paths=output_paths,
        ):
            continue
        filtered_validation.append(validation)
    task["validation"] = filtered_validation


def validation_command_repo_paths(command: str) -> list[str]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return []
    paths: list[str] = []
    for token in tokens:
        normalized = token.strip().lstrip("./")
        if not normalized:
            continue
        if looks_like_repo_path(normalized):
            paths.append(normalized)
    return dedupe_strings(paths)


def discovery_design_validation_is_redundant_self_check(
    command: str,
    *,
    output_paths: set[str],
) -> bool:
    if not output_paths:
        return False
    program = validation_command_program(command)
    if program not in {"test", "[", "rg", "grep"}:
        return False
    referenced_paths = validation_command_repo_paths(command)
    if not referenced_paths:
        return False
    return set(referenced_paths).issubset(output_paths)


def enforce_capability_owned_path_bounds(
    project_root: Path,
    objective_id: str,
    capability: str,
    owned_paths: list[str],
) -> None:
    if capability != "middleware":
        return
    app_root = find_objective_app_root(project_root, objective_id)
    if app_root is None:
        return
    disallowed_prefixes = [
        f"apps/{app_root.name}/frontend",
        f"apps/{app_root.name}/backend",
    ]
    violating = [
        owned_path
        for owned_path in owned_paths
        if any(owned_path_targets_prefix(owned_path, prefix) for prefix in disallowed_prefixes)
    ]
    if violating:
        joined = ", ".join(sorted(violating))
        raise ExecutorError(
            "Middleware/integration tasks may not own frontend or backend paths. "
            f"Move the change back to the capability owner instead. Invalid owned_paths: {joined}"
        )


def owned_path_targets_prefix(owned_path: str, prefix: str) -> bool:
    normalized = str(owned_path or "").strip().lstrip("./")
    normalized_prefix = str(prefix or "").strip().lstrip("./")
    if not normalized or not normalized_prefix:
        return False
    if any(character in normalized_prefix for character in "*?["):
        return fnmatch.fnmatch(normalized, normalized_prefix)
    return (
        normalized == normalized_prefix
        or normalized.startswith(f"{normalized_prefix}/")
        or normalized.startswith(f"{normalized_prefix}*")
    )


def owned_path_is_broad_superset_of_concrete_outputs(owned_path: str, concrete_output_paths: list[str]) -> bool:
    if not any(character in owned_path for character in "*?["):
        return False
    return any(path_pattern_conflict(owned_path, output_path) for output_path in concrete_output_paths)


def owned_paths_include_real_workspace_target(
    project_root: Path,
    owned_paths: list[str],
    concrete_output_paths: list[str],
) -> bool:
    generated_outputs = set(concrete_output_paths)
    for owned_path in owned_paths:
        if owned_path in generated_outputs:
            return True
        if owned_path.startswith("runs/"):
            continue
        if owned_path_target_exists(project_root, owned_path):
            return True
    return False


def owned_path_should_be_retained(project_root: Path, owned_path: str, concrete_output_paths: list[str]) -> bool:
    normalized = owned_path.strip()
    if not normalized:
        return False
    if normalized in set(concrete_output_paths):
        return True
    if normalized.startswith("runs/"):
        return True
    return owned_path_target_exists(project_root, normalized)


def owned_path_target_exists(project_root: Path, owned_path: str) -> bool:
    normalized = owned_path.strip()
    if not normalized:
        return False
    candidate_parts: list[str] = []
    for part in Path(normalized).parts:
        if any(character in part for character in "*?["):
            break
        candidate_parts.append(part)
    if not candidate_parts:
        return False
    candidate = project_root.joinpath(*candidate_parts)
    return candidate.exists()


def normalize_task_input_references(payload: dict[str, Any]) -> None:
    for task in payload.get("tasks", []):
        inputs = [value for value in task.get("inputs", []) if isinstance(value, str) and value.strip()]
        task["inputs"] = [canonicalize_input_reference(value) for value in inputs]


def canonicalize_same_phase_input_refs(
    project_root: Path,
    payload: dict[str, Any],
    *,
    objective_outline: dict[str, Any],
    capability: str,
) -> None:
    local_output_path_to_task_id = local_output_path_map(payload)
    inbound_path_to_ref = inbound_handoff_path_ref_map(objective_outline, capability=capability)
    for task in payload.get("tasks", []):
        canonical_inputs: list[str] = []
        for input_ref in task.get("inputs", []):
            if not isinstance(input_ref, str):
                continue
            normalized = input_ref.strip()
            if not normalized:
                continue
            if normalized.startswith(("Planning Inputs.", "Runtime Context.", "Output of ", "Outputs from ")):
                canonical_inputs.append(normalized)
                continue
            local_task_id = local_output_path_to_task_id.get(normalized)
            if local_task_id is not None and local_task_id != task["task_id"]:
                canonical_inputs.append(f"Output of {local_task_id}")
                continue
            inbound_ref = inbound_path_to_ref.get(normalized)
            if inbound_ref is not None:
                canonical_inputs.append(inbound_ref)
                continue
            canonical_inputs.append(normalized)
        task["inputs"] = dedupe_strings(canonical_inputs)


def reject_noncanonical_future_path_inputs(
    project_root: Path,
    payload: dict[str, Any],
    *,
    run_id: str,
    objective_outline: dict[str, Any],
    capability: str,
) -> None:
    local_output_paths = set(local_output_path_map(payload))
    inbound_paths = set(inbound_handoff_path_ref_map(objective_outline, capability=capability))
    for task in payload.get("tasks", []):
        task_local_existing_inputs = task_local_existing_input_paths(project_root, run_id, task)
        for input_ref in task.get("inputs", []):
            if not isinstance(input_ref, str):
                continue
            normalized = input_ref.strip()
            if not normalized or normalized.startswith(("Planning Inputs.", "Runtime Context.", "Output of ", "Outputs from ")):
                continue
            if normalized in task_local_existing_inputs:
                continue
            if normalized in local_output_paths or normalized in inbound_paths:
                raise ExecutorError(
                    f"Capability plan task {task['task_id']} must reference same-phase outputs via Output of ... or "
                    f"Planning Inputs.required_inbound_handoffs..., not literal path {normalized}"
                )
            if looks_like_repo_path(normalized) and not repo_input_exists_for_run(project_root, run_id, normalized):
                raise ExecutorError(
                    f"Capability plan task {task['task_id']} referenced nonexistent repo input path {normalized}. "
                    "Same-phase dependencies must use Output of ... or Planning Inputs.required_inbound_handoffs..."
                )


def task_local_existing_input_paths(project_root: Path, run_id: str, task: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for raw_path in task.get("writes_existing_paths", []):
        normalized = normalize_repo_relative_path(str(raw_path))
        if normalized:
            paths.add(normalized)
    for raw_path in task.get("owned_paths", []):
        normalized = normalize_repo_relative_path(str(raw_path))
        if normalized and repo_input_exists_for_run(project_root, run_id, normalized):
            paths.add(normalized)
    for output in normalize_output_descriptors(task.get("expected_outputs", [])):
        normalized = normalize_repo_relative_path(str(output.get("path") or ""))
        if normalized and repo_input_exists_for_run(project_root, run_id, normalized):
            paths.add(normalized)
    return paths


def repo_input_exists_for_run(project_root: Path, run_id: str, relative_path: str) -> bool:
    candidate = project_root / relative_path
    if candidate.exists():
        return True
    integration_candidate = integration_workspace_path(project_root, run_id) / relative_path
    return integration_candidate.exists()


def local_output_path_map(payload: dict[str, Any]) -> dict[str, str]:
    output_path_to_task_id: dict[str, str] = {}
    ambiguous_paths: set[str] = set()
    for task in payload.get("tasks", []):
        for descriptor in normalize_output_descriptors(list(task.get("expected_outputs", []))):
            path = descriptor_path(descriptor)
            if not path:
                continue
            existing = output_path_to_task_id.get(path)
            if existing is None:
                output_path_to_task_id[path] = task["task_id"]
            elif existing != task["task_id"]:
                ambiguous_paths.add(path)
    for path in ambiguous_paths:
        output_path_to_task_id.pop(path, None)
    return output_path_to_task_id


def inbound_handoff_path_ref_map(objective_outline: dict[str, Any], *, capability: str) -> dict[str, str]:
    inbound_edges = [
        edge
        for edge in objective_outline.get("collaboration_edges", [])
        if edge.get("to_capability") == capability and edge.get("from_capability") != capability
    ]
    refs: dict[str, str] = {}
    for edge_index, edge in enumerate(inbound_edges):
        for deliverable_index, descriptor in enumerate(normalize_output_descriptors(list(edge.get("deliverables", [])))):
            path = descriptor_path(descriptor)
            if not path:
                continue
            refs.setdefault(path, f"Planning Inputs.required_inbound_handoffs[{edge_index}].deliverables[{deliverable_index}]")
    return refs


def attach_required_inbound_handoff_assets(
    payload: dict[str, Any],
    *,
    objective_outline: dict[str, Any],
    capability: str,
) -> None:
    inbound_edges = [
        edge
        for edge in objective_outline.get("collaboration_edges", [])
        if edge.get("to_capability") == capability and edge.get("from_capability") != capability
    ]
    if not inbound_edges:
        return
    for task in payload.get("tasks", []):
        input_refs = [
            value
            for value in task.get("inputs", [])
            if isinstance(value, str) and value.startswith("Planning Inputs.required_inbound_handoffs")
        ]
        if not input_refs:
            continue
        selected_indexes: set[int] = set()
        include_all = False
        for input_ref in input_refs:
            matches = re.findall(r"required_inbound_handoffs(?:\[(\d+)\])?", input_ref)
            if not matches:
                include_all = True
                continue
            for matched_index in matches:
                if matched_index == "":
                    include_all = True
                    continue
                selected_indexes.add(int(matched_index))
        selected_edges = inbound_edges if include_all or not selected_indexes else [
            inbound_edges[index] for index in sorted(selected_indexes) if 0 <= index < len(inbound_edges)
        ]
        current_shared_assets = [value for value in task.get("shared_asset_ids", []) if isinstance(value, str) and value]
        for edge in selected_edges:
            current_shared_assets.extend(
                value for value in edge.get("shared_asset_ids", []) if isinstance(value, str) and value
            )
            current_shared_assets.extend(output_descriptor_ids(edge.get("deliverables", [])))
        task["shared_asset_ids"] = dedupe_strings(current_shared_assets)


def canonicalize_input_reference(input_ref: str) -> str:
    normalized = input_ref.strip()
    prefixes = ("Planning Inputs.", "Runtime Context.")
    for prefix in prefixes:
        if normalized.startswith(prefix):
            suffix = normalized.removeprefix(prefix)
            return prefix + canonicalize_dotted_numeric_segments(suffix)
    return normalized


def canonicalize_dotted_numeric_segments(path: str) -> str:
    if not path:
        return path
    path = re.sub(r"\[(?:\"([^\"]+)\"|'([^']+)')\]", lambda match: f".{match.group(1) or match.group(2)}", path)
    canonical_parts: list[str] = []
    for part in path.split("."):
        if part.isdigit() and canonical_parts:
            canonical_parts[-1] = f"{canonical_parts[-1]}[{part}]"
        else:
            canonical_parts.append(part)
    return ".".join(canonical_parts)


def task_mentions_shared_surface(task: dict[str, Any]) -> bool:
    haystack = " ".join(
        [str(task.get("objective", ""))]
        + [descriptor_summary(item) for item in normalize_output_descriptors(list(task.get("expected_outputs", [])))]
        + [str(item) for item in task.get("inputs", [])]
        + [str(item) for item in task.get("done_when", [])]
    ).lower()
    keywords = ("contract", "schema", "handoff", "integration", "shared", "interface")
    return any(keyword in haystack for keyword in keywords)


def normalize_collaboration_handoffs(payload: dict[str, Any], *, objective_id: str, capability: str) -> None:
    normalized: list[dict[str, Any]] = []
    for handoff in payload.get("collaboration_handoffs", []):
        item = dict(handoff)
        handoff_id = item["handoff_id"]
        if not handoff_id.startswith(f"{objective_id}-{capability}-"):
            handoff_id = f"{objective_id}-{capability}-{handoff_id}"
            item["handoff_id"] = handoff_id
        item["from_capability"] = capability
        item["to_role"] = normalize_role_reference(objective_id, item["to_role"])
        item["deliverable_output_ids"] = dedupe_strings(
            [str(value).strip() for value in item.get("deliverable_output_ids", []) if str(value).strip()]
        )
        shared_assets = [value for value in item.get("shared_asset_ids", []) if isinstance(value, str) and value]
        if handoff_id not in shared_assets:
            shared_assets.insert(0, handoff_id)
        item["shared_asset_ids"] = dedupe_strings(shared_assets)
        normalized.append(item)
    payload["collaboration_handoffs"] = normalized


def validate_required_handoffs(outline: dict[str, Any], handoffs: list[dict[str, Any]]) -> None:
    for edge in outline.get("collaboration_edges", []):
        matched = [
            handoff
            for handoff in handoffs
            if handoff["from_capability"] == edge["from_capability"]
            and handoff["to_capability"] == edge["to_capability"]
            and handoff["to_role"] == edge["to_role"]
            and handoff["handoff_type"] == edge["handoff_type"]
        ]
        if not matched:
            raise ExecutorError(
                "Capability plans did not materialize required collaboration edge "
                f"{edge['edge_id']} from {edge['from_capability']} to {edge['to_capability']}"
            )
        if len(matched) > 1:
            raise ExecutorError(
                f"Capability plans materialized required collaboration edge {edge['edge_id']} multiple times. "
                "Create one consolidation task and emit a single collaboration_handoff for the edge."
            )
        required_output_ids = set(output_descriptor_ids(edge.get("deliverables", [])))
        actual_output_ids = set(output_descriptor_ids(matched[0].get("deliverables", [])))
        missing_output_ids = sorted(required_output_ids - actual_output_ids)
        if missing_output_ids:
            raise ExecutorError(
                f"Capability plans did not fully materialize required collaboration edge {edge['edge_id']}: "
                f"missing deliverable outputs {', '.join(missing_output_ids)}. "
                "If the edge needs outputs from multiple tasks, create a final consolidation task and emit the "
                "handoff from that task."
            )


def materialize_capability_handoffs(tasks: list[dict[str, Any]], handoffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    task_by_id = {task["task_id"]: task for task in tasks}
    materialized: list[dict[str, Any]] = []
    for handoff in handoffs:
        source_task = task_by_id.get(handoff["from_task_id"])
        if source_task is None:
            raise ExecutorError(f"Capability plan collaboration handoff referenced unknown task {handoff['from_task_id']}")
        source_outputs = normalize_output_descriptors(list(source_task.get("expected_outputs", [])))
        source_output_map = {descriptor_output_id(item): item for item in source_outputs}
        deliverable_output_ids = [
            str(value).strip() for value in handoff.get("deliverable_output_ids", []) if str(value).strip()
        ]
        missing_output_ids = sorted(output_id for output_id in deliverable_output_ids if output_id not in source_output_map)
        if missing_output_ids:
            raise ExecutorError(
                f"Capability plan collaboration handoff {handoff['handoff_id']} references outputs not declared by "
                f"{handoff['from_task_id']}: {', '.join(missing_output_ids)}. "
                "Create a final consolidation task that emits the review bundle or handoff artifact, then emit the "
                "collaboration_handoff from that task."
            )
        item = {key: value for key, value in handoff.items() if key != "deliverable_output_ids"}
        item["deliverables"] = [source_output_map[output_id] for output_id in deliverable_output_ids]
        materialized.append(item)
    return materialized


def attach_handoff_shared_assets(tasks: list[dict[str, Any]], handoffs: list[dict[str, Any]]) -> None:
    task_by_id = {task["task_id"]: task for task in tasks}
    for handoff in handoffs:
        source_task = task_by_id.get(handoff["from_task_id"])
        if source_task is not None:
            source_task["shared_asset_ids"] = dedupe_strings(
                list(source_task.get("shared_asset_ids", [])) + list(handoff.get("shared_asset_ids", []))
            )
        for task in tasks:
            if task.get("capability") != handoff["to_capability"]:
                continue
            depends_on = set(task.get("depends_on", []))
            input_refs = {str(value) for value in task.get("inputs", [])}
            if handoff["from_task_id"] in depends_on or f"Output of {handoff['from_task_id']}" in input_refs:
                task["shared_asset_ids"] = dedupe_strings(
                    list(task.get("shared_asset_ids", [])) + list(handoff.get("shared_asset_ids", []))
                )


def attach_handoff_dependencies(tasks: list[dict[str, Any]], handoffs: list[dict[str, Any]]) -> None:
    task_by_id = {task["task_id"]: task for task in tasks}
    objective_id = next((task.get("objective_id") for task in tasks if task.get("objective_id")), None)
    for task in tasks:
        task.setdefault("handoff_dependencies", [])
    for handoff in handoffs:
        resolved_handoff = dict(handoff)
        if objective_id is not None:
            resolved_handoff.setdefault("objective_id", objective_id)
        target_ids = derive_target_tasks(resolved_handoff, task_by_id)
        handoff["to_task_ids"] = target_ids
        for task_id in target_ids:
            task = task_by_id.get(task_id)
            if task is None:
                continue
            task["handoff_dependencies"] = dedupe_strings(
                list(task.get("handoff_dependencies", [])) + [handoff["handoff_id"]]
            )


def validate_capability_plan_contents(
    project_root: Path,
    plan: dict[str, Any],
    *,
    run_id: str,
    phase: str,
    objective_id: str,
    capability: str,
    objective_outline: dict[str, Any],
) -> None:
    canonicalize_planned_task_worker_roles(
        plan,
        objective_id=objective_id,
        default_capability=capability,
    )
    valid_roles = {f"objectives.{objective_id}.general-worker"}
    if capability != "general":
        valid_roles.add(f"objectives.{objective_id}.{capability}-worker")
    lane = next(
        (item for item in objective_outline.get("capability_lanes", []) if item.get("capability") == capability),
        None,
    )
    required_lane_output_ids = (
        {descriptor_output_id(item) for item in normalize_output_descriptors(lane.get("expected_outputs", []))}
        if lane is not None
        else set()
    )
    task_ids = set()
    handoff_ids = set()
    produced_output_ids: set[str] = set()
    for task in plan["tasks"]:
        if task["task_id"] in task_ids:
            raise ExecutorError(f"Capability plan duplicated task id {task['task_id']}")
        task_ids.add(task["task_id"])
        task_outputs = normalize_output_descriptors(task.get("expected_outputs", []))
        if not task_outputs:
            raise ExecutorError(
                f"Capability plan task {task['task_id']} must declare at least one expected output. "
                "Read-only reconciliation tasks should emit an assertion output."
            )
        produced_output_ids.update(descriptor_output_id(item) for item in task_outputs)
        if task["assigned_role"] not in valid_roles:
            raise ExecutorError(f"Capability plan assigned unknown worker role {task['assigned_role']}")
        if task.get("capability") not in {capability, "general", None}:
            raise ExecutorError(
                f"Capability plan task {task['task_id']} used mismatched capability {task.get('capability')}"
            )
        if task["execution_mode"] == "isolated_write" and not task["owned_paths"]:
            raise ExecutorError(
                f"Capability plan must declare owned_paths for isolated_write task {task['task_id']}"
            )
        validate_task_assertion_evidence(task, plan_label="Capability plan")
    validate_discovery_design_producing_task_contracts(
        plan["tasks"],
        phase=phase,
        plan_label="Capability plan",
    )
    validate_capability_plan_contract_authority(plan["tasks"], capability=capability)
    missing_lane_output_ids = sorted(required_lane_output_ids - produced_output_ids)
    if missing_lane_output_ids:
        raise ExecutorError(
            "Capability plan tasks did not cover all capability lane expected outputs: "
            + ", ".join(missing_lane_output_ids)
            + ". Add the missing outputs to producing tasks, or create one final consolidation task that emits them."
        )
    for bundle in plan["bundle_plan"]:
        for task_id in bundle["task_ids"]:
            if task_id not in task_ids:
                raise ExecutorError(f"Bundle {bundle['bundle_id']} referenced unknown task {task_id}")
    for handoff in plan["collaboration_handoffs"]:
        if handoff["handoff_id"] in handoff_ids:
            raise ExecutorError(f"Capability plan duplicated handoff id {handoff['handoff_id']}")
        handoff_ids.add(handoff["handoff_id"])
        if handoff["from_task_id"] not in task_ids:
            raise ExecutorError(
                f"Capability plan collaboration handoff referenced unknown task {handoff['from_task_id']}"
            )
        if handoff["from_capability"] != capability:
            raise ExecutorError(
                f"Capability plan collaboration handoff {handoff['handoff_id']} used mismatched source capability"
            )
        if not handoff["deliverable_output_ids"]:
            raise ExecutorError(
                f"Capability plan collaboration handoff {handoff['handoff_id']} must declare deliverable_output_ids"
            )
        source_task = next(task for task in plan["tasks"] if task["task_id"] == handoff["from_task_id"])
        source_output_ids = {descriptor_output_id(item) for item in normalize_output_descriptors(source_task["expected_outputs"])}
        handoff_output_ids = {str(value).strip() for value in handoff.get("deliverable_output_ids", []) if str(value).strip()}
        missing_output_ids = sorted(handoff_output_ids - source_output_ids)
        if missing_output_ids:
            raise ExecutorError(
                f"Capability plan collaboration handoff {handoff['handoff_id']} references outputs not declared by "
                f"{handoff['from_task_id']}: {', '.join(missing_output_ids)}. "
                "Create a final consolidation task that emits the review bundle or handoff artifact, then emit the "
                "collaboration_handoff from that task."
            )
    validate_capability_required_handoffs(plan, objective_outline=objective_outline, capability=capability)
    validate_phase_task_graph_shape(plan, objective_outline=objective_outline, capability=capability, phase=phase)
    validate_validation_commands(
        plan["tasks"],
        project_root=project_root,
        phase=phase,
        plan_label="Capability plan",
        objective_id=objective_id,
        capability=capability,
    )
    validate_backend_mvp_build_input_authority(plan["tasks"], phase=phase, capability=capability)
    for task in plan["tasks"]:
        validate_prior_artifact_path_continuity(
            project_root,
            run_id,
            objective_id=objective_id,
            capability=capability,
            phase=phase,
            descriptors=list(task.get("expected_outputs", [])),
            owner_label=f"Capability plan task {task['task_id']}",
        )
    validate_backend_persistence_alignment(
        project_root,
        plan,
        run_id=run_id,
        phase=phase,
        objective_id=objective_id,
        capability=capability,
    )
    validate_capability_repo_shape_alignment(
        project_root,
        plan,
        run_id=run_id,
        phase=phase,
        objective_id=objective_id,
        capability=capability,
    )


def validate_capability_required_handoffs(
    plan: dict[str, Any],
    *,
    objective_outline: dict[str, Any],
    capability: str,
) -> None:
    task_by_id = {task["task_id"]: task for task in plan["tasks"]}
    required_edges = [
        edge for edge in objective_outline.get("collaboration_edges", []) if edge.get("from_capability") == capability
    ]
    for edge in required_edges:
        matched = [
            handoff
            for handoff in plan.get("collaboration_handoffs", [])
            if handoff.get("to_capability") == edge.get("to_capability")
            and handoff.get("to_role") == edge.get("to_role")
            and handoff.get("handoff_type") == edge.get("handoff_type")
        ]
        if not matched:
            raise ExecutorError(
                "Capability plan did not materialize required outbound collaboration edge "
                f"{edge['edge_id']} from {edge['from_capability']} to {edge['to_capability']}"
            )
        if len(matched) > 1:
            raise ExecutorError(
                f"Capability plan materialized required outbound collaboration edge {edge['edge_id']} multiple times. "
                "Create one consolidation task and emit a single collaboration_handoff for the edge."
            )
        handoff = matched[0]
        source_task = task_by_id[handoff["from_task_id"]]
        source_output_ids = {
            descriptor_output_id(item) for item in normalize_output_descriptors(source_task.get("expected_outputs", []))
        }
        required_output_ids = set(output_descriptor_ids(edge.get("deliverables", [])))
        handoff_output_ids = {
            str(value).strip() for value in handoff.get("deliverable_output_ids", []) if str(value).strip()
        }
        unavailable_output_ids = sorted(required_output_ids - source_output_ids)
        if unavailable_output_ids:
            raise ExecutorError(
                f"Required outbound collaboration edge {edge['edge_id']} needs outputs that are not declared by "
                f"{handoff['from_task_id']}: {', '.join(unavailable_output_ids)}. "
                "Create a final consolidation task that emits the review bundle or handoff artifact, then emit the "
                "collaboration_handoff from that task."
            )
        missing_output_ids = sorted(required_output_ids - handoff_output_ids)
        if missing_output_ids:
            raise ExecutorError(
                f"Capability plan collaboration handoff {handoff['handoff_id']} did not reference all required outputs "
            f"for edge {edge['edge_id']}: {', '.join(missing_output_ids)}"
            )


def validate_phase_task_graph_shape(
    plan: dict[str, Any],
    *,
    objective_outline: dict[str, Any],
    capability: str,
    phase: str,
) -> None:
    if phase not in {"discovery", "design"}:
        return
    lane = next(
        (item for item in objective_outline.get("capability_lanes", []) if item.get("capability") == capability),
        None,
    )
    required_output_ids: set[str] = set()
    if lane is not None:
        required_output_ids.update(output_descriptor_ids(lane.get("expected_outputs", [])))
    for edge in objective_outline.get("collaboration_edges", []):
        if edge.get("from_capability") != capability:
            continue
        required_output_ids.update(output_descriptor_ids(edge.get("deliverables", [])))
    if not required_output_ids:
        return
    handoff_source_ids = {
        str(handoff.get("from_task_id", "")).strip()
        for handoff in plan.get("collaboration_handoffs", [])
        if isinstance(handoff.get("from_task_id"), str) and str(handoff.get("from_task_id")).strip()
    }
    for task in plan.get("tasks", []):
        task_output_ids = {
            descriptor_output_id(item)
            for item in normalize_output_descriptors(list(task.get("expected_outputs", [])))
        }
        if task_output_ids & required_output_ids:
            continue
        if task.get("task_id") in handoff_source_ids:
            continue
        raise ExecutorError(
            f"{phase} capability plan task {task['task_id']} is internal-only: it does not emit any required lane "
            "outputs or required outbound handoff outputs. Discovery/design lanes should emit final artifacts and "
            "handoff outputs directly from producing tasks instead of separate synthesis or packaging-only steps."
        )


_VALIDATION_SHELL_BUILTINS = {
    "[",
    "alias",
    "cd",
    "echo",
    "eval",
    "exec",
    "exit",
    "export",
    "false",
    "printf",
    "pwd",
    "read",
    "set",
    "test",
    "true",
    "unset",
}


def validation_command_program(command: str) -> str | None:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    for token in tokens:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", token):
            continue
        return token
    return None


def validation_command_is_resolvable(command: str) -> bool:
    program = validation_command_program(command)
    if not program:
        return False
    if program in _VALIDATION_SHELL_BUILTINS:
        return True
    if "/" in program:
        return True
    return shutil.which(program) is not None


def validation_command_prefix_path(command: str) -> str | None:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    for index, token in enumerate(tokens[:-1]):
        if token != "--prefix":
            continue
        candidate = tokens[index + 1].strip().lstrip("./")
        if candidate:
            return candidate
    return None


def validation_command_prefix_issue(project_root: Path, command: str) -> str | None:
    prefix_path = validation_command_prefix_path(command)
    if not prefix_path:
        return None
    candidate = project_root / prefix_path
    if not candidate.exists():
        return f"uses --prefix `{prefix_path}` but that directory does not exist"
    package_manifest = candidate / "package.json"
    if not package_manifest.exists():
        return (
            f"uses --prefix `{prefix_path}` but `{prefix_path}/package.json` does not exist"
        )
    return None


def validate_validation_commands(
    tasks: list[dict[str, Any]],
    *,
    project_root: Path,
    phase: str,
    plan_label: str,
    objective_id: str | None = None,
    capability: str | None = None,
) -> None:
    hints = (
        validation_environment_hints_for_capability(
            project_root,
            objective_id=objective_id,
            capability=capability,
            phase=phase,
        )
        if objective_id and capability
        else {}
    )
    for task in tasks:
        for validation in task.get("validation", []):
            command = validation.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ExecutorError(
                    f"{plan_label} task {task['task_id']} declared an empty validation command"
                )
            if not validation_command_is_resolvable(command):
                raise ExecutorError(
                    f"{plan_label} task {task['task_id']} declared validation command `{command}` "
                    "that does not start with a real executable or shell builtin. "
                    "Use a real command instead of a placeholder validator."
                )
            prefix_issue = validation_command_prefix_issue(project_root, command)
            if prefix_issue:
                raise ExecutorError(
                    f"{plan_label} task {task['task_id']} declared validation command `{command}` that "
                    f"{prefix_issue}. Use the real package root or a direct repo-root validation command."
                )
            catalog_issue = validation_command_catalog_issue(command, hints=hints)
            if catalog_issue:
                raise ExecutorError(
                    f"{plan_label} task {task['task_id']} declared validation command `{command}` that "
                    f"{catalog_issue}. Use the Validation Command Catalog for this capability."
                )


def validate_backend_mvp_build_input_authority(tasks: list[dict[str, Any]], *, phase: str, capability: str) -> None:
    if phase != "mvp-build" or capability != "backend":
        return
    for task in tasks:
        conflicting_inputs = sorted(
            {
                normalized
                for value in task.get("inputs", [])
                if isinstance(value, str)
                for normalized in [value.strip()]
                if normalized and is_frontend_consumption_contract_path(normalized)
            }
        )
        if conflicting_inputs:
            raise ExecutorError(
                f"Capability plan task {task['task_id']} must not consume frontend-api-consumption-contract.md "
                "during backend mvp-build. Use the approved backend design/OpenAPI package and middleware "
                "reconciled integration contract as authoritative API inputs instead."
            )


def validate_capability_plan_contract_authority(tasks: list[dict[str, Any]], *, capability: str) -> None:
    for task in tasks:
        validate_contract_authority_for_descriptors(
            list(task.get("expected_outputs", [])),
            capability=capability,
            owner_label=f"Capability plan task {task['task_id']}",
        )
        validate_nonfrontend_consumer_contract_inputs(
            list(task.get("inputs", [])),
            capability=capability,
            owner_label=f"Capability plan task {task['task_id']}",
        )


def validate_discovery_design_producing_task_contracts(
    tasks: list[dict[str, Any]],
    *,
    phase: str,
    plan_label: str,
) -> None:
    if phase not in {"discovery", "design"}:
        return
    for task in tasks:
        if task.get("execution_mode") != "isolated_write":
            continue
        outputs = normalize_output_descriptors(list(task.get("expected_outputs", [])))
        file_outputs = [
            item
            for item in outputs
            if descriptor_kind(item) in {"artifact", "asset"} and descriptor_path(item)
        ]
        if not file_outputs:
            continue
        assertion_output_ids = [
            descriptor_output_id(item)
            for item in outputs
            if descriptor_kind(item) == "assertion"
        ]
        if assertion_output_ids:
            raise ExecutorError(
                f"{plan_label} task {task['task_id']} is a discovery/design producing task and should not declare "
                "task-level assertion outputs in addition to concrete file outputs. Keep only the files/assets it creates: "
                + ", ".join(sorted(assertion_output_ids))
            )
        output_paths = {
            str(descriptor_path(item)).strip().lstrip("./")
            for item in file_outputs
            if descriptor_path(item)
        }
        for validation in task.get("validation", []):
            command = validation.get("command")
            if isinstance(command, str) and discovery_design_validation_is_redundant_self_check(
                command,
                output_paths=output_paths,
            ):
                raise ExecutorError(
                    f"{plan_label} task {task['task_id']} declared redundant self-validation `{command}` "
                    "over files it creates. Discovery/design producing tasks should write the declared artifacts "
                    "directly and leave content review to acceptance."
                )


def align_required_outbound_handoff_output_ids(
    plan: dict[str, Any],
    *,
    objective_outline: dict[str, Any],
    capability: str,
) -> None:
    task_by_id = {task["task_id"]: task for task in plan.get("tasks", [])}
    required_edges = [
        edge for edge in objective_outline.get("collaboration_edges", []) if edge.get("from_capability") == capability
    ]
    for edge in required_edges:
        matched = [
            handoff
            for handoff in plan.get("collaboration_handoffs", [])
            if handoff.get("to_capability") == edge.get("to_capability")
            and handoff.get("to_role") == edge.get("to_role")
            and handoff.get("handoff_type") == edge.get("handoff_type")
        ]
        if len(matched) != 1:
            continue
        handoff = matched[0]
        source_task = task_by_id.get(str(handoff.get("from_task_id", "")).strip())
        if source_task is None:
            continue
        source_output_ids = {
            descriptor_output_id(item) for item in normalize_output_descriptors(source_task.get("expected_outputs", []))
        }
        combined = [
            str(value).strip() for value in handoff.get("deliverable_output_ids", []) if str(value).strip()
        ]
        combined.extend(
            output_id
            for output_id in output_descriptor_ids(edge.get("deliverables", []))
            if output_id in source_output_ids
        )
        handoff["deliverable_output_ids"] = dedupe_strings(combined)


def validate_backend_persistence_alignment(
    project_root: Path,
    plan: dict[str, Any],
    *,
    run_id: str,
    phase: str,
    objective_id: str,
    capability: str,
) -> None:
    if phase != "mvp-build" or capability != "backend":
        return
    run_dir = project_root / "runs" / run_id
    related_reports = collect_related_app_prior_phase_reports(project_root, run_dir, objective_id, capability, phase)
    approved_signals = " ".join(str(report.get("summary", "")) for report in related_reports).lower()
    if "sqlite" not in approved_signals:
        return
    serialized_plan = json.dumps(plan, sort_keys=True).lower()
    conflicting_signals = [
        "todos.json",
        "json-file",
        "json file",
        "file-backed repository",
        "file-backed persistence",
    ]
    if any(signal in serialized_plan for signal in conflicting_signals):
        raise ExecutorError(
            "Capability plan contradicted the approved backend persistence stack: related prior-phase backend reports "
            "lock the app to SQLite, but the emitted mvp-build backend plan introduced JSON-file persistence work."
        )


def detect_capability_workspace_language(
    project_root: Path,
    *,
    run_id: str,
    objective_id: str,
    capability: str,
    phase: str,
) -> str | None:
    app_root = find_objective_app_root(project_root, objective_id)
    if app_root is None:
        return None
    workspace_root = capability_workspace_root(app_root, capability, phase=phase)
    if workspace_root is None:
        return None
    try:
        relative_workspace_root = workspace_root.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    roots = [project_root]
    integration_root = integration_workspace_path(project_root, run_id)
    if integration_root.exists():
        roots.append(integration_root)
    js_files = 0
    ts_files = 0
    seen: set[str] = set()
    for root in roots:
        candidate_root = root / relative_workspace_root
        if not candidate_root.exists():
            continue
        for candidate in candidate_root.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                relative_path = str(candidate.relative_to(root)).replace("\\", "/")
            except ValueError:
                continue
            if relative_path in seen:
                continue
            seen.add(relative_path)
            if candidate.suffix in {".js", ".jsx", ".cjs", ".mjs"}:
                js_files += 1
            elif candidate.suffix in {".ts", ".tsx", ".cts", ".mts"}:
                ts_files += 1
    if js_files and not ts_files:
        return "javascript"
    if ts_files and not js_files:
        return "typescript"
    return None


def validate_capability_repo_shape_alignment(
    project_root: Path,
    plan: dict[str, Any],
    *,
    run_id: str,
    phase: str,
    objective_id: str,
    capability: str,
) -> None:
    if phase != "mvp-build" or capability not in {"frontend", "backend"}:
        return
    established_language = detect_capability_workspace_language(
        project_root,
        run_id=run_id,
        objective_id=objective_id,
        capability=capability,
        phase=phase,
    )
    if established_language is None:
        return
    app_root = find_objective_app_root(project_root, objective_id)
    if app_root is None:
        return
    workspace_root = capability_workspace_root(app_root, capability, phase=phase)
    if workspace_root is None:
        return
    try:
        workspace_prefix = str(workspace_root.resolve().relative_to(project_root.resolve())).replace("\\", "/")
    except ValueError:
        return
    invalid_suffixes = {".ts", ".tsx", ".cts", ".mts"} if established_language == "javascript" else {".js", ".jsx", ".cjs", ".mjs"}
    candidate_paths: list[str] = []
    for task in plan.get("tasks", []):
        candidate_paths.extend(str(value).strip() for value in task.get("owned_paths", []) if str(value).strip())
        candidate_paths.extend(str(value).strip() for value in task.get("writes_existing_paths", []) if str(value).strip())
        candidate_paths.extend(
            path_value
            for descriptor in normalize_output_descriptors(list(task.get("expected_outputs", [])))
            if (path_value := descriptor_path(descriptor))
        )
        for validation in task.get("validation", []):
            command = validation.get("command")
            if isinstance(command, str) and command.strip():
                candidate_paths.extend(validation_command_repo_paths(command))
    conflicts = [
        normalized_path
        for path_value in dedupe_strings(candidate_paths)
        if (normalized_path := normalize_repo_relative_path(path_value))
        and owned_path_targets_prefix(normalized_path, workspace_prefix)
        and Path(normalized_path).suffix in invalid_suffixes
    ]
    if conflicts:
        raise ExecutorError(
            f"Capability plan contradicted the observed {capability} workspace language: existing {capability} code is "
            f"{established_language}, but the plan introduced incompatible implementation paths or validation targets: "
            + ", ".join(sorted(conflicts))
        )


def validate_objective_plan_contents(project_root: Path, plan: dict[str, Any], objective: dict[str, Any]) -> None:
    canonicalize_planned_task_worker_roles(
        plan,
        objective_id=str(objective["objective_id"]),
        default_capability=None,
    )
    valid_roles = {f"objectives.{objective['objective_id']}.general-worker"}
    for capability in objective["capabilities"]:
        if capability != "general":
            valid_roles.add(f"objectives.{objective['objective_id']}.{capability}-worker")
    task_ids = set()
    handoff_ids = set()
    for task in plan["tasks"]:
        if task["task_id"] in task_ids:
            raise ExecutorError(f"Objective plan duplicated task id {task['task_id']}")
        task_ids.add(task["task_id"])
        if not normalize_output_descriptors(task.get("expected_outputs", [])):
            raise ExecutorError(
                f"Objective plan task {task['task_id']} must declare at least one expected output. "
                "Read-only reconciliation tasks should emit an assertion output."
            )
        if task["assigned_role"] not in valid_roles:
            raise ExecutorError(f"Objective plan assigned unknown worker role {task['assigned_role']}")
        if task["execution_mode"] == "isolated_write" and not task["owned_paths"]:
            raise ExecutorError(
                f"Objective plan must declare owned_paths for isolated_write task {task['task_id']}"
            )
        validate_task_assertion_evidence(task, plan_label="Objective plan")
    validate_discovery_design_producing_task_contracts(
        plan["tasks"],
        phase=str(plan.get("phase", "")),
        plan_label="Objective plan",
    )
    for bundle in plan["bundle_plan"]:
        for task_id in bundle["task_ids"]:
            if task_id not in task_ids:
                raise ExecutorError(f"Bundle {bundle['bundle_id']} referenced unknown task {task_id}")
    for handoff in plan["collaboration_handoffs"]:
        if handoff["handoff_id"] in handoff_ids:
            raise ExecutorError(f"Objective plan duplicated handoff id {handoff['handoff_id']}")
        handoff_ids.add(handoff["handoff_id"])
        if handoff["from_task_id"] not in task_ids:
            raise ExecutorError(f"Collaboration handoff referenced unknown task {handoff['from_task_id']}")


def validate_planned_task_inputs(
    project_root: Path,
    run_id: str,
    phase: str,
    objective_id: str,
    tasks: list[dict[str, Any]],
) -> None:
    canonicalize_planned_task_worker_roles(
        {"tasks": tasks},
        objective_id=objective_id,
        default_capability=None,
    )
    planned_task_ids = {task["task_id"] for task in tasks}
    for planned_task in tasks:
        preview_task = {
            "schema": "task-assignment.v1",
            "run_id": run_id,
            "phase": phase,
            "objective_id": objective_id,
            "capability": planned_task["capability"],
            "working_directory": planned_task["working_directory"],
            "sandbox_mode": planned_task["sandbox_mode"],
            "additional_directories": planned_task["additional_directories"],
            "execution_mode": planned_task["execution_mode"],
            "parallel_policy": planned_task["parallel_policy"],
            "owned_paths": planned_task["owned_paths"],
            "writes_existing_paths": planned_task.get("writes_existing_paths", []),
            "shared_asset_ids": planned_task["shared_asset_ids"],
            "handoff_dependencies": planned_task.get("handoff_dependencies", []),
            "task_id": planned_task["task_id"],
            "assigned_role": planned_task["assigned_role"],
            "manager_role": derive_manager_role(project_root, objective_id, planned_task["assigned_role"]),
            "acceptance_role": f"objectives.{objective_id}.acceptance-manager",
            "objective": planned_task["objective"],
            "inputs": planned_task["inputs"],
            "expected_outputs": planned_task["expected_outputs"],
            "done_when": planned_task["done_when"],
            "depends_on": planned_task["depends_on"],
            "validation": planned_task["validation"],
            "collaboration_rules": planned_task["collaboration_rules"],
        }
        unresolved = sorted(collect_unresolved_input_refs(preview_resolved_inputs(project_root, run_id, preview_task)))
        unresolved = [
            input_ref
            for input_ref in unresolved
            if not is_known_planned_task_output_ref(input_ref, planned_task_ids)
        ]
        if unresolved:
            raise ExecutorError(
                f"Objective plan produced unresolved input refs for task {planned_task['task_id']}: "
                + ", ".join(unresolved)
            )
        compiled_context = compile_task_context_packet(
            project_root,
            run_id,
            preview_task,
            files_loaded=[],
            prompt_path="",
            role_kind="worker",
        )
        missing_inputs = list(compiled_context.get("missing_inputs", []))
        if missing_inputs:
            details = []
            for item in missing_inputs:
                input_ref = str(item.get("input_ref") or "").strip()
                reason = str(item.get("reason") or "").strip()
                detail = str(item.get("detail") or "").strip()
                details.append(f"{input_ref} ({reason}: {detail})" if detail else f"{input_ref} ({reason})")
            raise ExecutorError(
                f"Objective plan produced non-materializable task inputs for task {planned_task['task_id']}: "
                + ", ".join(details)
            )


def validate_task_assertion_evidence(task: dict[str, Any], *, plan_label: str) -> None:
    task_validation_ids = {
        str(item.get("id")).strip()
        for item in task.get("validation", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str) and str(item.get("id")).strip()
    }
    for output in normalize_output_descriptors(task.get("expected_outputs", [])):
        if output.get("kind") != "assertion":
            continue
        evidence = output.get("evidence", {})
        validation_ids = [
            str(item).strip()
            for item in (evidence.get("validation_ids", []) if isinstance(evidence, dict) else [])
            if isinstance(item, str) and str(item).strip()
        ]
        missing = sorted(set(validation_ids) - task_validation_ids)
        if missing:
            raise ExecutorError(
                f"{plan_label} task {task['task_id']} assertion output {descriptor_output_id(output)} "
                "references validations that are not declared on the task: "
                + ", ".join(missing)
            )


def collect_unresolved_input_refs(value: Any) -> set[str]:
    unresolved: set[str] = set()
    if isinstance(value, dict):
        unresolved_ref = value.get("unresolved_input_ref")
        if isinstance(unresolved_ref, str):
            unresolved.add(unresolved_ref)
        missing_path = value.get("missing_path")
        if isinstance(missing_path, str):
            unresolved.add(missing_path)
        missing_task_output = value.get("missing_task_output")
        if isinstance(missing_task_output, str):
            unresolved.add(f"Output of {missing_task_output}")
        missing_section = value.get("missing_section")
        if isinstance(missing_section, str):
            unresolved.add(missing_section)
        for nested in value.values():
            unresolved.update(collect_unresolved_input_refs(nested))
    elif isinstance(value, list):
        for nested in value:
            unresolved.update(collect_unresolved_input_refs(nested))
    return unresolved


def is_known_planned_task_output_ref(input_ref: str, planned_task_ids: set[str]) -> bool:
    if not input_ref.startswith("Output of "):
        return False
    return input_ref.removeprefix("Output of ").strip() in planned_task_ids


def materialize_objective_plan(project_root: Path, run_id: str, plan: dict[str, Any], *, replace: bool) -> None:
    run_dir = project_root / "runs" / run_id
    phase = plan["phase"]
    objective_id = plan["objective_id"]
    canonicalize_planned_task_worker_roles(
        plan,
        objective_id=objective_id,
        default_capability=None,
    )
    existing_paths = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        payload = read_json(path)
        if payload["phase"] == phase and payload["objective_id"] == objective_id:
            existing_paths.append(path)
    desired_payloads: dict[str, dict[str, Any]] = {}
    planned_output_paths: set[str] = set()
    for planned_task in plan["tasks"]:
        normalize_task_execution_entry(
            project_root,
            objective_id,
            planned_task["capability"],
            planned_task,
            phase_override=phase,
            run_id=run_id,
            default_sandbox_mode=str(planned_task.get("sandbox_mode", "read-only")),
            available_existing_paths=planned_output_paths,
        )
        planned_output_paths.update(concrete_expected_output_paths(planned_task))
        payload = {
            "schema": "task-assignment.v1",
            "run_id": run_id,
            "phase": phase,
            "objective_id": objective_id,
            "capability": planned_task["capability"],
            "working_directory": planned_task["working_directory"],
            "sandbox_mode": planned_task["sandbox_mode"],
            "additional_directories": planned_task["additional_directories"],
            "execution_mode": planned_task["execution_mode"],
            "parallel_policy": planned_task["parallel_policy"],
            "owned_paths": planned_task["owned_paths"],
            "writes_existing_paths": planned_task.get("writes_existing_paths", []),
            "shared_asset_ids": planned_task["shared_asset_ids"],
            "handoff_dependencies": planned_task.get("handoff_dependencies", []),
            "task_id": planned_task["task_id"],
            "assigned_role": planned_task["assigned_role"],
            "manager_role": derive_manager_role(project_root, objective_id, planned_task["assigned_role"]),
            "acceptance_role": f"objectives.{objective_id}.acceptance-manager",
            "objective": planned_task["objective"],
            "inputs": planned_task["inputs"],
            "expected_outputs": planned_task["expected_outputs"],
            "done_when": planned_task["done_when"],
            "depends_on": planned_task["depends_on"],
            "validation": planned_task["validation"],
            "collaboration_rules": planned_task["collaboration_rules"],
        }
        validate_document(payload, "task-assignment.v1", project_root)
        desired_payloads[planned_task["task_id"]] = payload

    manager_plan_path = run_dir / "manager-plans" / f"{phase}-{objective_id}.json"
    collaboration_dir = ensure_dir(run_dir / "collaboration-plans")
    existing_handoff_paths = []
    for path in sorted(collaboration_dir.glob("*.json")):
        payload = read_json(path)
        if payload["phase"] == phase and payload["objective_id"] == objective_id:
            existing_handoff_paths.append(path)
    desired_handoffs = {
        handoff["handoff_id"]: build_planned_handoff_payload(project_root, run_id, phase, objective_id, handoff)
        for handoff in plan.get("collaboration_handoffs", [])
    }
    if existing_paths and not replace:
        existing_payloads = {path.stem: read_json(path) for path in existing_paths}
        existing_handoffs = {path.stem: read_json(path) for path in existing_handoff_paths}
        if set(existing_payloads) == set(desired_payloads) and set(existing_handoffs) == set(desired_handoffs) and all(
            existing_payloads[task_id] == desired_payloads[task_id] for task_id in desired_payloads
        ) and all(existing_handoffs[handoff_id] == desired_handoffs[handoff_id] for handoff_id in desired_handoffs):
            write_json(manager_plan_path, plan)
            return
        raise ExecutorError(f"Tasks already exist for objective {objective_id} in phase {phase}; rerun with replace")

    for path in existing_paths:
        path.unlink()
    for path in existing_handoff_paths:
        path.unlink()

    write_json(manager_plan_path, plan)

    for task_id, payload in desired_payloads.items():
        write_json(run_dir / "tasks" / f"{task_id}.json", payload)
    for handoff_id, payload in desired_handoffs.items():
        write_json(collaboration_dir / f"{handoff_id}.json", payload)
    write_objective_task_graph_manifest(
        run_dir,
        phase=phase,
        objective_id=objective_id,
        task_ids=sorted(desired_payloads),
        bundle_ids=sorted(str(bundle.get("bundle_id") or "").strip() for bundle in plan.get("bundle_plan", []) if str(bundle.get("bundle_id") or "").strip()),
        handoff_ids=sorted(desired_handoffs),
    )


def build_planned_handoff_payload(
    project_root: Path, run_id: str, phase: str, objective_id: str, handoff: dict[str, Any]
) -> dict[str, Any]:
    payload = {
        "schema": "collaboration-handoff.v1",
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "handoff_id": handoff["handoff_id"],
        "from_capability": handoff["from_capability"],
        "to_capability": handoff["to_capability"],
        "from_task_id": handoff["from_task_id"],
        "to_role": handoff["to_role"],
        "handoff_type": handoff["handoff_type"],
        "reason": handoff["reason"],
        "deliverables": handoff["deliverables"],
        "blocking": handoff["blocking"],
        "shared_asset_ids": handoff["shared_asset_ids"],
        "to_task_ids": handoff.get("to_task_ids", []),
        "status": "planned",
        "satisfied_by_task_ids": [],
        "missing_deliverables": [],
        "status_reason": None,
        "last_checked_at": None,
    }
    validate_document(payload, "collaboration-handoff.v1", project_root)
    return payload


def derive_manager_role_for_capability(project_root: Path, objective_id: str, capability: str) -> str:
    if capability == "general":
        return f"objectives.{objective_id}.objective-manager"
    candidate_role = f"{capability}-manager"
    candidate_path = find_objective_root(project_root, objective_id) / "approved" / f"{candidate_role}.md"
    if candidate_path.exists():
        return f"objectives.{objective_id}.{candidate_role}"
    return f"objectives.{objective_id}.objective-manager"


def resolve_capability_manager_role(objective_outline: dict[str, Any], capability: str) -> str:
    lane = next(item for item in objective_outline["capability_lanes"] if item["capability"] == capability)
    return lane["assigned_manager_role"]


def derive_manager_role(project_root: Path, objective_id: str, assigned_role: str) -> str:
    role_name = assigned_role.split(".")[-1]
    if role_name == "general-worker":
        return f"objectives.{objective_id}.objective-manager"
    candidate_role_name = role_name.replace("-worker", "-manager")
    candidate_path = find_objective_root(project_root, objective_id) / "approved" / f"{candidate_role_name}.md"
    if candidate_path.exists():
        return f"objectives.{objective_id}.{candidate_role_name}"
    return f"objectives.{objective_id}.objective-manager"


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def dedupe_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for value in values:
        marker = json.dumps(value, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result
