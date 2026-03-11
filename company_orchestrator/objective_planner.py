from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .executor import (
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
from .filesystem import ensure_dir, load_optional_json, read_json, write_json, write_text
from .live import capability_plan_activity_id, ensure_activity, now_timestamp, plan_activity_id, record_event, update_activity
from .objective_roots import find_objective_root
from .parallelism import infer_execution_metadata
from .prompts import preview_resolved_inputs, render_capability_planning_prompt, render_objective_planning_prompt
from .recovery import prepare_activity_retry, reconcile_for_command
from .schemas import SchemaValidationError, validate_document


class PlanningLimiter:
    def __init__(self, max_concurrency: int) -> None:
        self.max_concurrency = max(1, max_concurrency)
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._waiting = 0

    def acquire(self) -> int | None:
        if self._semaphore.acquire(blocking=False):
            return None
        with self._lock:
            self._waiting += 1
            queue_position = self._waiting
        self._semaphore.acquire()
        with self._lock:
            self._waiting = max(0, self._waiting - 1)
        return queue_position

    def release(self) -> None:
        self._semaphore.release()


def plan_objective(
    project_root: Path,
    run_id: str,
    objective_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    replace: bool = False,
    timeout_seconds: int = 300,
    max_concurrency: int = 3,
    allow_recovery_blocked: bool = False,
    skip_reconcile: bool = False,
    planning_limiter: PlanningLimiter | None = None,
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
    outline = None if replace else load_valid_document(project_root, outline_path, "objective-outline.v1")
    if outline is None:
        objective_prompt = render_objective_planning_prompt(project_root, run_id, objective_id)
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
            execution_prompt=build_planning_prompt(
                (project_root / objective_prompt["prompt_path"]).read_text(encoding="utf-8")
            ),
            output_schema_name="objective-outline.v1",
            output_prefix=f"{phase}-{objective_id}",
            failure_label=f"objective {objective_id}",
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            planning_limiter=planning_limiter,
        )
        outline, identity_adjustments = normalize_objective_outline(
            project_root,
            objective_result["payload"],
            run_id=run_id,
            phase=phase,
            objective=objective,
        )
        objective_result["identity_adjustments"].update(identity_adjustments)
        write_json(outline_path, outline)
    else:
        objective_result["recovery_action"] = "reused_valid_outline"
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
    )
    plan = aggregate_capability_plans(
        project_root,
        run_id,
        phase,
        objective_id,
        outline,
        capability_plans,
    )
    planning_mode = "capability_managed"

    validate_objective_plan_contents(project_root, plan, objective)
    validate_planned_task_inputs(project_root, run_id, plan["phase"], plan["objective_id"], plan["tasks"])
    materialize_objective_plan(project_root, run_id, plan, replace=replace)

    summary = {
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "thread_id": extract_thread_id(objective_result["events"]),
        "usage": extract_usage(objective_result["events"]),
        "plan_path": f"runs/{run_id}/manager-plans/{phase}-{objective_id}.json",
        "task_ids": [task["task_id"] for task in plan["tasks"]],
        "bundle_ids": [bundle["bundle_id"] for bundle in plan["bundle_plan"]],
        "stdout_path": objective_result["stdout_path"],
        "stderr_path": objective_result["stderr_path"],
        "last_message_path": objective_result["last_message_path"],
        "identity_adjustments": objective_result["identity_adjustments"],
        "planning_mode": planning_mode,
        "capability_summaries": capability_summaries,
        "attempt": objective_result["attempt"],
        "recovery_action": objective_result["recovery_action"],
        "max_concurrency": max_concurrency,
    }
    write_json(plans_dir / f"{phase}-{objective_id}.summary.json", summary)
    activity_status = "recovered" if objective_result["attempt"] > 1 or objective_result["recovery_action"] else "completed"
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
        output_path=f"runs/{run_id}/manager-plans/{phase}-{objective_id}.json",
        process_metadata=None,
        recovered_at=now_timestamp() if activity_status == "recovered" else None,
        recovery_action=objective_result["recovery_action"],
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
            "recovery_action": objective_result["recovery_action"],
        },
    )
    return summary


def plan_phase(
    project_root: Path,
    run_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    replace: bool = False,
    timeout_seconds: int = 300,
    max_concurrency: int = 3,
) -> dict[str, Any]:
    reconcile_for_command(project_root, run_id, apply=True)
    run_dir = project_root / "runs" / run_id
    phase = read_json(run_dir / "phase-plan.json")["current_phase"]
    objective_map = read_json(run_dir / "objective-map.json")
    planning_limiter = PlanningLimiter(max_concurrency)
    summaries = []
    objective_ids = [objective["objective_id"] for objective in objective_map["objectives"]]
    if max_concurrency <= 1 or len(objective_ids) <= 1:
        for objective_id in objective_ids:
            summaries.append(
                plan_objective(
                    project_root,
                    run_id,
                    objective_id,
                    sandbox_mode=sandbox_mode,
                    codex_path=codex_path,
                    replace=replace,
                    timeout_seconds=timeout_seconds,
                    max_concurrency=max_concurrency,
                    skip_reconcile=True,
                    planning_limiter=planning_limiter,
                )
            )
    else:
        summaries_by_objective: dict[str, dict[str, Any]] = {}
        first_error: BaseException | None = None
        with ThreadPoolExecutor(max_workers=min(len(objective_ids), max_concurrency)) as pool:
            futures = {
                pool.submit(
                    plan_objective,
                    project_root,
                    run_id,
                    objective_id,
                    sandbox_mode=sandbox_mode,
                    codex_path=codex_path,
                    replace=replace,
                    timeout_seconds=timeout_seconds,
                    max_concurrency=max_concurrency,
                    skip_reconcile=True,
                    planning_limiter=planning_limiter,
                ): objective_id
                for objective_id in objective_ids
            }
            for future in as_completed(futures):
                objective_id = futures[future]
                try:
                    summaries_by_objective[objective_id] = future.result()
                except BaseException as exc:  # pragma: no cover - exercised in failure tests via raise below
                    if first_error is None:
                        first_error = exc
        if first_error is not None:
            raise first_error
        summaries = [summaries_by_objective[objective_id] for objective_id in objective_ids]
    payload = {
        "run_id": run_id,
        "phase": phase,
        "planned_objectives": summaries,
        "max_concurrency": max_concurrency,
    }
    write_json(run_dir / "manager-plans" / f"{phase}-phase-plan-summary.json", payload)
    return payload


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
    timeout_seconds: int,
    max_concurrency: int,
    planning_limiter: PlanningLimiter,
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
    timeout_seconds: int,
    planning_limiter: PlanningLimiter,
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
    plan_path = run_dir / "manager-plans" / f"{phase}-{objective_id}-{capability}.json"
    plan = None if replace else load_valid_document(project_root, plan_path, "capability-plan.v1")
    if plan is None:
        prompt_metadata = render_capability_planning_prompt(
            project_root,
            run_id,
            objective_id,
            capability,
            objective_outline,
        )
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
            execution_prompt=build_capability_planning_prompt(
                (project_root / prompt_metadata["prompt_path"]).read_text(encoding="utf-8")
            ),
            output_schema_name="capability-plan.v1",
            output_prefix=f"{phase}-{objective_id}-{capability}",
            failure_label=f"{objective_id}:{capability}",
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            planning_limiter=planning_limiter,
        )
        plan, identity_adjustments = normalize_capability_plan(
            project_root,
            result["payload"],
            run_id=run_id,
            phase=phase,
            objective_id=objective_id,
            capability=capability,
            objective_outline=objective_outline,
        )
        result["identity_adjustments"].update(identity_adjustments)
    else:
        result["recovery_action"] = "reused_valid_capability_plan"
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
    timeout_seconds: int,
    planning_limiter: PlanningLimiter,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    plans_dir = ensure_dir(run_dir / "manager-plans")
    output_schema_path = project_root / "orchestrator" / "schemas" / f"{output_schema_name}.json"
    last_message_path = plans_dir / f"{output_prefix}.last-message.json"
    stdout_path = plans_dir / f"{output_prefix}.stdout.jsonl"
    stderr_path = plans_dir / f"{output_prefix}.stderr.log"
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
        attempt=attempt,
        begin_attempt=previous_activity is not None,
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
    queue_position = planning_limiter.acquire()
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
            )
            record_event(
                project_root,
                run_id,
                phase=phase,
                activity_id=activity_id,
                event_type="planning.queued",
                message=f"Queued planning activity for {failure_label}.",
                payload={"entity_id": entity_id, "queue_position": queue_position},
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

        def on_stdout_line(raw_line: str) -> None:
            handle_codex_event_line(project_root, run_id, phase, activity_id, raw_line)

        def on_process_started(process: subprocess.Popen[str]) -> None:
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

        try:
            completed = run_codex_command(
                command,
                prompt=execution_prompt,
                cwd=project_root,
                env=build_exec_environment(),
                timeout_seconds=timeout_seconds,
                on_stdout_line=on_stdout_line,
                on_process_started=on_process_started,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = coerce_process_text(exc.stdout)
            stderr = coerce_process_text(exc.stderr)
            write_text(stdout_path, stdout)
            write_text(stderr_path, stderr)
            update_activity(
                project_root,
                run_id,
                activity_id,
                status="failed",
                progress_stage="failed",
                current_activity=f"Timed out after {timeout_seconds} seconds.",
                process_metadata=None,
            )
            record_event(
                project_root,
                run_id,
                phase=phase,
                activity_id=activity_id,
                event_type="planning.failed",
                message=f"Planning activity for {failure_label} timed out.",
                payload={"timeout_seconds": timeout_seconds},
            )
            raise ExecutorError(f"codex exec timed out after {timeout_seconds} seconds while planning {failure_label}") from exc

        write_text(stdout_path, completed.stdout)
        write_text(stderr_path, completed.stderr)
        events = parse_jsonl_events(completed.stdout)
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

        final_response = extract_final_response(events)
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
        "recovery_action": "retry" if previous_activity is not None else None,
    }


def load_valid_document(project_root: Path, path: Path, schema_name: str) -> dict[str, Any] | None:
    payload = load_optional_json(path)
    if payload is None:
        return None
    try:
        validate_document(payload, schema_name, project_root)
    except SchemaValidationError:
        return None
    return payload


def find_objective(run_dir: Path, objective_id: str) -> dict[str, Any]:
    objective_map = read_json(run_dir / "objective-map.json")
    for objective in objective_map["objectives"]:
        if objective["objective_id"] == objective_id:
            return objective
    raise ValueError(f"Objective {objective_id} was not found")


def build_planning_prompt(prompt_text: str) -> str:
    return (
        prompt_text
        + "\n\n# Objective Planning Output Requirements\n\n"
        + "Return only one JSON object matching the objective-outline schema.\n"
        + "Do not wrap the JSON in markdown fences.\n"
        + "Copy run_id, phase, and objective_id exactly from the injected Runtime Context and Planning Inputs.\n"
        + "Use only the Runtime Context and Planning Inputs already provided in this prompt.\n"
        + "Do not inspect the repository, run shell commands, or read additional files.\n"
        + "Do not perform exploratory analysis outside the injected planning inputs.\n"
        + "Return the JSON plan as your first and only response.\n"
        + "Do not execute implementation work.\n"
        + "Define capability lanes for the active objective using only roles already present in the injected team definition.\n"
        + "Each capability lane must include objective, inputs, expected_outputs, done_when, depends_on, planning_notes, and collaboration_rules.\n"
        + "Use collaboration_edges only for real cross-lane dependencies that require another role.\n"
    )


def build_capability_planning_prompt(prompt_text: str) -> str:
    return (
        prompt_text
        + "\n\n# Capability Planning Output Requirements\n\n"
        + "Return only one JSON object matching the capability-plan schema.\n"
        + "Do not wrap the JSON in markdown fences.\n"
        + "Copy run_id, phase, objective_id, and capability exactly from the injected Runtime Context and Capability Planning Inputs.\n"
        + "Use only the Runtime Context and Capability Planning Inputs already provided in this prompt.\n"
        + "Do not inspect the repository, run shell commands, or read additional files.\n"
        + "Return the JSON plan as your first and only response.\n"
        + "Do not execute implementation work.\n"
        + "Produce small isolated worker tasks for this capability lane only.\n"
        + "Use only worker roles from the listed objective team when assigning tasks.\n"
        + "Every generated task must include execution_mode, parallel_policy, owned_paths, and shared_asset_ids.\n"
        + "Use execution_mode `read_only` for analysis/reporting work and `isolated_write` for code-writing or file-writing work.\n"
        + "Use parallel_policy `allow` only when you can justify safe isolation from other tasks; otherwise use `serialize`.\n"
        + "Every bundle in bundle_plan must reference only generated task ids.\n"
        + "For phases after discovery, each task input must be either a concrete repo-relative file path, "
        + "an explicit `Output of <task-id>` reference, or a dotted `Planning Inputs.`/`Runtime Context.` reference.\n"
        + "When prior-phase reports or artifacts are available in Capability Planning Inputs, prefer referencing those exact paths "
        + "instead of vague English placeholders such as 'approved design package'.\n"
    )


def normalize_plan_identity(
    payload: dict[str, Any], *, run_id: str, phase: str, objective_id: str
) -> dict[str, dict[str, str]]:
    if payload["phase"] != phase or payload["objective_id"] != objective_id:
        raise ExecutorError("Planning output identity does not match the requested objective/phase")
    adjustments: dict[str, dict[str, str]] = {}
    if payload["run_id"] != run_id:
        adjustments["run_id"] = {"from": str(payload["run_id"]), "to": run_id}
        payload["run_id"] = run_id
    return adjustments


def normalize_objective_outline(
    project_root: Path,
    payload: dict[str, Any],
    *,
    run_id: str,
    phase: str,
    objective: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    try:
        validate_document(payload, "objective-outline.v1", project_root)
    except SchemaValidationError as exc:
        raise ExecutorError(f"Objective manager returned invalid objective outline: {exc}") from exc
    adjustments = normalize_plan_identity(
        payload,
        run_id=run_id,
        phase=phase,
        objective_id=objective["objective_id"],
    )
    expected_capabilities = list(objective.get("capabilities", [])) or ["general"]
    seen: set[str] = set()
    for lane in payload["capability_lanes"]:
        capability = lane["capability"]
        if capability in seen:
            raise ExecutorError(f"Objective outline duplicated capability lane {capability}")
        seen.add(capability)
        if capability not in expected_capabilities:
            raise ExecutorError(
                f"Objective outline proposed unexpected capability lane {capability} for objective {objective['objective_id']}"
            )
        expected_manager_role = derive_manager_role_for_capability(project_root, objective["objective_id"], capability)
        if lane["assigned_manager_role"] != expected_manager_role:
            lane["assigned_manager_role"] = expected_manager_role
    if not payload["capability_lanes"]:
        raise ExecutorError("Objective outline must include at least one capability lane")
    return payload, adjustments


def normalize_capability_plan(
    project_root: Path,
    payload: dict[str, Any],
    *,
    run_id: str,
    phase: str,
    objective_id: str,
    capability: str,
    objective_outline: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    try:
        validate_document(payload, "capability-plan.v1", project_root)
    except SchemaValidationError as exc:
        raise ExecutorError(f"Capability manager returned invalid capability plan: {exc}") from exc
    if payload["capability"] != capability:
        raise ExecutorError(f"Capability plan identity does not match requested capability {capability}")
    adjustments = normalize_plan_identity(payload, run_id=run_id, phase=phase, objective_id=objective_id)
    normalize_task_execution_metadata(payload)
    normalize_bundle_ids(payload)
    validate_capability_plan_contents(project_root, payload, objective_id=objective_id, capability=capability)
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
    collaboration_edges: list[dict[str, Any]] = []

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
        collaboration_edges.extend(plan.get("collaboration_edges", []))

    for edge in outline.get("collaboration_edges", []):
        source_tasks = [task["task_id"] for task in tasks if task.get("capability") == edge["from_capability"]]
        for task_id in source_tasks:
            collaboration_edges.append(
                {
                    "from_task_id": task_id,
                    "to_role": edge["to_role"],
                    "reason": edge["reason"],
                }
            )

    plan = {
        "schema": "objective-plan.v1",
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "summary": outline["summary"],
        "tasks": tasks,
        "bundle_plan": bundle_plan,
        "dependency_notes": dedupe_strings(dependency_notes),
        "collaboration_edges": dedupe_dicts(collaboration_edges),
    }
    try:
        validate_document(plan, "objective-plan.v1", project_root)
    except SchemaValidationError as exc:
        raise ExecutorError(f"Aggregated capability plan was invalid: {exc}") from exc
    normalize_bundle_ids(plan)
    return plan


def normalize_bundle_ids(payload: dict[str, Any]) -> None:
    objective_id = payload["objective_id"]
    seen: set[str] = set()
    for bundle in payload.get("bundle_plan", []):
        bundle_id = bundle["bundle_id"]
        if not bundle_id.startswith(f"{objective_id}-"):
            bundle_id = f"{objective_id}-{bundle_id}"
            bundle["bundle_id"] = bundle_id
        if bundle_id in seen:
            raise ExecutorError(f"Planning output duplicated bundle id {bundle_id}")
        seen.add(bundle_id)


def normalize_task_execution_metadata(payload: dict[str, Any]) -> None:
    phase = str(payload.get("phase", "discovery"))
    for task in payload.get("tasks", []):
        inferred = infer_execution_metadata(
            phase=phase,
            task_id=str(task.get("task_id", "")),
            expected_outputs=task.get("expected_outputs", []),
            existing=task,
        )
        task.update(inferred)
        task.setdefault("working_directory", None)
        task.setdefault("sandbox_mode", "read-only")
        task.setdefault("additional_directories", [])


def validate_capability_plan_contents(
    project_root: Path,
    plan: dict[str, Any],
    *,
    objective_id: str,
    capability: str,
) -> None:
    valid_roles = {f"objectives.{objective_id}.general-worker"}
    if capability != "general":
        valid_roles.add(f"objectives.{objective_id}.{capability}-worker")
    task_ids = set()
    for task in plan["tasks"]:
        if task["task_id"] in task_ids:
            raise ExecutorError(f"Capability plan duplicated task id {task['task_id']}")
        task_ids.add(task["task_id"])
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
    for bundle in plan["bundle_plan"]:
        for task_id in bundle["task_ids"]:
            if task_id not in task_ids:
                raise ExecutorError(f"Bundle {bundle['bundle_id']} referenced unknown task {task_id}")
    for edge in plan["collaboration_edges"]:
        if edge["from_task_id"] not in task_ids:
            raise ExecutorError(
                f"Capability plan collaboration edge referenced unknown task {edge['from_task_id']}"
            )


def validate_objective_plan_contents(project_root: Path, plan: dict[str, Any], objective: dict[str, Any]) -> None:
    valid_roles = {f"objectives.{objective['objective_id']}.general-worker"}
    for capability in objective["capabilities"]:
        if capability != "general":
            valid_roles.add(f"objectives.{objective['objective_id']}.{capability}-worker")
    task_ids = set()
    for task in plan["tasks"]:
        if task["task_id"] in task_ids:
            raise ExecutorError(f"Objective plan duplicated task id {task['task_id']}")
        task_ids.add(task["task_id"])
        if task["assigned_role"] not in valid_roles:
            raise ExecutorError(f"Objective plan assigned unknown worker role {task['assigned_role']}")
        if task["execution_mode"] == "isolated_write" and not task["owned_paths"]:
            raise ExecutorError(
                f"Objective plan must declare owned_paths for isolated_write task {task['task_id']}"
            )
    for bundle in plan["bundle_plan"]:
        for task_id in bundle["task_ids"]:
            if task_id not in task_ids:
                raise ExecutorError(f"Bundle {bundle['bundle_id']} referenced unknown task {task_id}")
    for edge in plan["collaboration_edges"]:
        if edge["from_task_id"] not in task_ids:
            raise ExecutorError(f"Collaboration edge referenced unknown task {edge['from_task_id']}")


def validate_planned_task_inputs(
    project_root: Path,
    run_id: str,
    phase: str,
    objective_id: str,
    tasks: list[dict[str, Any]],
) -> None:
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
            "shared_asset_ids": planned_task["shared_asset_ids"],
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
        if unresolved:
            raise ExecutorError(
                f"Objective plan produced unresolved input refs for task {planned_task['task_id']}: "
                + ", ".join(unresolved)
            )


def collect_unresolved_input_refs(value: Any) -> set[str]:
    unresolved: set[str] = set()
    if isinstance(value, dict):
        unresolved_ref = value.get("unresolved_input_ref")
        if isinstance(unresolved_ref, str):
            unresolved.add(unresolved_ref)
        for nested in value.values():
            unresolved.update(collect_unresolved_input_refs(nested))
    elif isinstance(value, list):
        for nested in value:
            unresolved.update(collect_unresolved_input_refs(nested))
    return unresolved


def materialize_objective_plan(project_root: Path, run_id: str, plan: dict[str, Any], *, replace: bool) -> None:
    run_dir = project_root / "runs" / run_id
    phase = plan["phase"]
    objective_id = plan["objective_id"]
    existing_paths = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        payload = read_json(path)
        if payload["phase"] == phase and payload["objective_id"] == objective_id:
            existing_paths.append(path)
    desired_payloads: dict[str, dict[str, Any]] = {}
    for planned_task in plan["tasks"]:
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
            "shared_asset_ids": planned_task["shared_asset_ids"],
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
    if existing_paths and not replace:
        existing_payloads = {path.stem: read_json(path) for path in existing_paths}
        if set(existing_payloads) == set(desired_payloads) and all(
            existing_payloads[task_id] == desired_payloads[task_id] for task_id in desired_payloads
        ):
            write_json(manager_plan_path, plan)
            return
        raise ExecutorError(f"Tasks already exist for objective {objective_id} in phase {phase}; rerun with replace")

    for path in existing_paths:
        path.unlink()

    write_json(manager_plan_path, plan)

    for task_id, payload in desired_payloads.items():
        write_json(run_dir / "tasks" / f"{task_id}.json", payload)


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
