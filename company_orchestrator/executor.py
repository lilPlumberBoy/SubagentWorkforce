from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .changes import normalize_change_request_payloads, persist_change_requests
from .collaboration import create_collaboration_request
from .filesystem import append_text, clear_text, ensure_dir, read_json, read_text, write_json
from .handoffs import collect_handoff_artifact_paths
from .input_lineage import build_task_input_source_metadata
from .impact import apply_approved_change_impacts
from .live import (
    append_activity_warning,
    ensure_activity,
    note_activity_stream,
    now_timestamp,
    read_activity,
    record_event,
    update_activity_observability_timestamps,
    update_activity,
)
from .observability import compact_observability_for_report, prompt_metrics, record_llm_call
from .output_descriptors import (
    descriptor_output_id,
    normalize_output_descriptors,
    output_descriptor_map,
    repo_relative_path_exists,
)
from .parallelism import (
    canonicalize_validation_commands,
    effective_sandbox_mode,
    normalize_task_artifact_descriptors,
    task_requires_write_access,
)
from .prompts import (
    compile_task_context_packet,
    load_input_artifact_for_run,
    load_task_repair_context,
    preview_resolved_inputs,
    render_prompt,
    resolve_report_artifact_path,
    resolve_workspace_input_path,
)
from .recovery import prepare_activity_retry, reconcile_for_command
from .schemas import SchemaValidationError, validate_document
from .task_graph import infer_task_runtime_requirements, load_task_runtime_contract
from .timeout_policy import resolve_task_timeout_policy, timeout_final_message, timeout_retry_message
from .worktree_manager import (
    WorkspaceInfo,
    WorktreeError,
    commit_task_workspace,
    ensure_task_workspace_with_refresh,
    integration_workspace_path,
)


class ExecutorError(RuntimeError):
    pass


@dataclass
class CodexProcessResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class CodexProcessStall(RuntimeError):
    cmd: list[str]
    stall_seconds: int
    reason: str
    output: str
    stderr: str

    def __str__(self) -> str:
        return f"codex exec stalled after {self.stall_seconds} seconds ({self.reason})"


@dataclass
class TaskExecutionRuntime:
    parallel_execution_requested: bool = False
    parallel_execution_granted: bool = False
    parallel_fallback_reason: str | None = None
    runtime_warnings: list[dict[str, str]] = field(default_factory=list)
    branch_name: str | None = None
    workspace_path: str | None = None
    commit_sha: str | None = None
    attempt: int = 1
    recovery_action: str | None = None
    workspace_reused: bool = False


def apply_repair_context_to_task(task: dict[str, Any], repair_context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(repair_context, dict):
        return task
    if str(repair_context.get("source") or "").strip() not in {"run_state_repair", "bundle_broad_retry"} and not bool(
        repair_context.get("allow_broadening_scope")
    ):
        return task
    focus_paths = [
        str(value).strip()
        for value in list(repair_context.get("focus_paths") or [])
        if isinstance(value, str) and str(value).strip() and not Path(str(value).strip()).is_absolute()
    ]
    if not focus_paths:
        return task
    adjusted = dict(task)
    writes_existing_paths = [
        str(value).strip()
        for value in list(adjusted.get("writes_existing_paths") or [])
        if isinstance(value, str) and str(value).strip()
    ]
    owned_paths = [
        str(value).strip()
        for value in list(adjusted.get("owned_paths") or [])
        if isinstance(value, str) and str(value).strip()
    ]
    adjusted["writes_existing_paths"] = sorted({*writes_existing_paths, *focus_paths})
    adjusted["owned_paths"] = sorted({*owned_paths, *focus_paths})
    return adjusted


def _copy_authoritative_file(source_path: Path, destination_path: Path) -> bool:
    if not source_path.exists() or not source_path.is_file():
        return False
    ensure_dir(destination_path.parent)
    if destination_path.exists() and source_path.read_bytes() == destination_path.read_bytes():
        return False
    shutil.copy2(source_path, destination_path)
    return True


def apply_run_state_repair_preflight(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    runtime: TaskExecutionRuntime,
    repair_context: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(repair_context, dict):
        return []
    if str(repair_context.get("source") or "").strip() != "run_state_repair":
        return []
    workspace_root = Path(str(runtime.workspace_path)).resolve() if runtime.workspace_path else None
    integration_root = integration_workspace_path(project_root, run_id)
    focus_paths = [
        str(value).strip()
        for value in list(repair_context.get("focus_paths") or [])
        if isinstance(value, str) and str(value).strip() and not str(value).strip().startswith("runs/")
    ]
    repaired: set[str] = set()
    for relative_path in focus_paths:
        source_candidates: list[Path] = []
        repo_path = (project_root / relative_path).resolve()
        source_candidates.append(repo_path)
        if workspace_root is not None:
            source_candidates.append((workspace_root / relative_path).resolve())
        source_candidates.append((integration_root / relative_path).resolve())
        source_path = next((candidate for candidate in source_candidates if candidate.exists() and candidate.is_file()), None)
        if source_path is None:
            continue
        if _copy_authoritative_file(source_path, (integration_root / relative_path).resolve()):
            repaired.add(relative_path)
        if workspace_root is not None and _copy_authoritative_file(source_path, (workspace_root / relative_path).resolve()):
            repaired.add(relative_path)
    if repaired:
        runtime.recovery_action = runtime.recovery_action or "run_state_repair"
    return sorted(repaired)


def task_declared_file_paths(task: dict[str, Any]) -> list[str]:
    paths: set[str] = set()
    for key in ("owned_paths", "writes_existing_paths"):
        for value in task.get(key, []):
            normalized = str(value).strip()
            if normalized and not Path(normalized).is_absolute() and not normalized.startswith("runs/"):
                paths.add(normalized)
    for output in task.get("expected_outputs", []):
        if not isinstance(output, dict):
            continue
        normalized = str(output.get("path") or "").strip()
        if normalized and not Path(normalized).is_absolute() and not normalized.startswith("runs/"):
            paths.add(normalized)
    return sorted(paths)


def command_tokens(command: str) -> list[str]:
    try:
        outer = shlex.split(command)
    except ValueError:
        return [command]
    if len(outer) >= 3 and outer[1] == "-lc":
        try:
            return shlex.split(outer[2])
        except ValueError:
            return [outer[2]]
    return outer


def extract_repo_relative_paths_from_command(command: str) -> list[str]:
    paths: list[str] = []
    for token in command_tokens(command):
        normalized = str(token).strip().strip("\"'")
        if (
            "/" not in normalized
            or normalized.startswith(("/", "-", "runs/"))
            or "$" in normalized
            or normalized.startswith(".orchestrator-")
        ):
            continue
        if normalized.endswith((";", ",", ")")):
            normalized = normalized.rstrip(";,)")
        if normalized:
            paths.append(normalized)
    return sorted({path for path in paths if path})


def looks_like_validation_command(command: str) -> bool:
    lowered = command.lower()
    if "validate:" in lowered:
        return True
    tokens = command_tokens(command)
    joined = " ".join(tokens).lower()
    if not joined:
        return False
    validation_markers = (
        "npm test",
        "pnpm test",
        "yarn test",
        "node --test",
        "pytest",
        "cargo test",
        "go test",
    )
    return any(marker in joined for marker in validation_markers)


def infer_attempt_run_state_repair_context(
    task: dict[str, Any],
    attempt_events: list[dict[str, Any]],
    *,
    existing_repair_context: dict[str, Any] | None,
    trigger_reason: str,
) -> dict[str, Any] | None:
    if isinstance(existing_repair_context, dict) and str(existing_repair_context.get("source") or "").strip() == "run_state_repair":
        return existing_repair_context
    declared_validation_commands = [
        str(item.get("command") or "").strip()
        for item in task.get("validation", [])
        if isinstance(item, dict) and str(item.get("command") or "").strip()
    ]
    validation_started = False
    run_report_accessed = False
    observed_paths: set[str] = set()
    for event in attempt_events:
        if event.get("type") != "item.started":
            continue
        item = event.get("item", {})
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        command = str(item.get("command") or "").strip()
        if not command:
            continue
        if any(validation_command in command for validation_command in declared_validation_commands) or looks_like_validation_command(command):
            validation_started = True
        if "runs/" in command and ("/reports/" in command or ".json" in command):
            run_report_accessed = True
        observed_paths.update(extract_repo_relative_paths_from_command(command))
    declared_paths = set(task_declared_file_paths(task))
    extra_paths = sorted(path for path in observed_paths if path not in declared_paths)
    if not validation_started and not (
        str(trigger_reason or "").startswith("stall_") and run_report_accessed and extra_paths
    ):
        return None
    if not extra_paths:
        return None
    focus_paths = sorted({*declared_paths, *extra_paths})
    return {
        "source": "run_state_repair",
        "summary": "Repair stale run-local state before retrying validation-heavy task execution.",
        "task_id": str(task.get("task_id") or "").strip(),
        "objective_id": str(task.get("objective_id") or "").strip(),
        "trigger_reason": trigger_reason,
        "focus_paths": focus_paths,
    }


MAX_TASK_EXECUTION_MISSING_FINAL_MESSAGE_RETRIES = 1
TASK_STALL_TIMEOUT_MIN_SECONDS = 60
TASK_STALL_TIMEOUT_MAX_SECONDS = 180


def coerce_process_text(stream: str | bytes | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


def task_stall_timeout_seconds(timeout_seconds: int) -> int:
    return max(
        1,
        min(
            timeout_seconds,
            max(
                TASK_STALL_TIMEOUT_MIN_SECONDS,
                min(TASK_STALL_TIMEOUT_MAX_SECONDS, max(1, timeout_seconds // 4)),
            ),
        ),
    )


def run_codex_command(
    command: list[str],
    *,
    prompt: str,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    on_stdout_line: Any | None = None,
    on_stderr_line: Any | None = None,
    on_process_started: Any | None = None,
    stall_timeout_seconds: int | None = None,
    stall_reason: Any | None = None,
) -> CodexProcessResult:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        cwd=cwd,
        env=env,
    )
    if on_process_started is not None:
        on_process_started(process)
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def consume_stdout() -> None:
        assert process.stdout is not None
        for raw_line in process.stdout:
            stdout_chunks.append(raw_line)
            if on_stdout_line is not None:
                on_stdout_line(raw_line.rstrip("\n"))

    def consume_stderr() -> None:
        assert process.stderr is not None
        for raw_line in process.stderr:
            stderr_chunks.append(raw_line)
            if on_stderr_line is not None:
                on_stderr_line(raw_line)

    stdout_thread = threading.Thread(target=consume_stdout, daemon=True)
    stderr_thread = threading.Thread(target=consume_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    def finalize_process() -> tuple[str, str]:
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        return "".join(stdout_chunks), "".join(stderr_chunks)

    try:
        assert process.stdin is not None
        process.stdin.write(prompt)
        process.stdin.close()
        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd=command, timeout=timeout_seconds)
            try:
                process.wait(timeout=min(1.0, remaining))
                break
            except subprocess.TimeoutExpired as exc:
                if stall_timeout_seconds is not None and stall_reason is not None:
                    reason = stall_reason()
                    if reason:
                        process.kill()
                        process.wait()
                        stdout_text, stderr_text = finalize_process()
                        raise CodexProcessStall(
                            cmd=command,
                            stall_seconds=stall_timeout_seconds,
                            reason=reason,
                            output=stdout_text,
                            stderr=stderr_text,
                        ) from exc
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        stdout_text, stderr_text = finalize_process()
        raise subprocess.TimeoutExpired(
            cmd=command,
            timeout=timeout_seconds,
            output=stdout_text,
            stderr=stderr_text,
        ) from exc

    stdout_text, stderr_text = finalize_process()
    return CodexProcessResult(
        returncode=process.returncode,
        stdout=stdout_text,
        stderr=stderr_text,
    )


def build_task_execution_materials(
    project_root: Path,
    run_id: str,
    task_path: Path,
    task: dict[str, Any],
    *,
    sandbox_mode: str,
    runtime: TaskExecutionRuntime,
) -> tuple[Path, str, dict[str, Any], str]:
    task_working_directory = runtime.workspace_path or task.get("working_directory")
    if task_working_directory:
        working_directory = Path(task_working_directory)
        if not working_directory.is_absolute():
            working_directory = (project_root / working_directory).resolve()
        else:
            working_directory = working_directory.resolve()
    else:
        working_directory = project_root
    task_sandbox_mode = effective_sandbox_mode(task, sandbox_mode)
    role_kind = "worker"
    task_context = compile_task_context_packet(
        project_root,
        run_id,
        task,
        files_loaded=[],
        prompt_path=str((project_root / "runs" / run_id / "prompt-logs" / f"{task['task_id']}.prompt.md").relative_to(project_root)),
        role_kind=role_kind,
        working_directory=working_directory,
        sandbox_mode=task_sandbox_mode,
    )
    validate_compiled_task_context(task_context)
    materialized_read_paths = materialize_task_context_files(project_root, run_id, task, working_directory)
    task_context["materialized_read_paths"] = materialized_read_paths
    prompt_metadata = render_prompt(
        project_root,
        run_id,
        task_path,
        working_directory=working_directory,
        sandbox_mode=task_sandbox_mode,
        task_payload=task,
        compiled_task_context=task_context,
    )
    prompt_text = read_text(project_root / prompt_metadata["prompt_path"])
    execution_prompt = build_execution_prompt(prompt_text)
    return working_directory, task_sandbox_mode, prompt_metadata, execution_prompt


def execute_task(
    project_root: Path,
    run_id: str,
    task_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    timeout_seconds: int | None = None,
    runtime: TaskExecutionRuntime | None = None,
    allow_recovery_blocked: bool = False,
) -> dict[str, Any]:
    reconcile_for_command(project_root, run_id, apply=True, allow_blocked=allow_recovery_blocked)
    run_dir = project_root / "runs" / run_id
    task_path = run_dir / "tasks" / f"{task_id}.json"
    if not task_path.exists():
        raise ExecutorError(f"Task {task_id} does not exist for run {run_id}")

    repair_context = load_task_repair_context(project_root, run_id, task_id)
    task = apply_repair_context_to_task(read_json(task_path), repair_context)
    normalize_task_artifact_descriptors(task)
    canonicalize_validation_commands(task)
    runtime_requirements = infer_task_runtime_requirements(task)
    task["execution_mode"] = resolve_task_execution_mode(task, runtime_requirements)
    write_json(task_path, task)
    runtime = runtime or TaskExecutionRuntime()
    timeout_policy = resolve_task_timeout_policy(task["phase"], task.get("execution_mode", "read_only"), timeout_seconds)
    previous_activity = prepare_activity_retry(
        project_root,
        run_id,
        task_id,
        reason="Starting a new execution attempt.",
    )
    if previous_activity is not None:
        runtime.attempt = int(previous_activity.get("attempt", 1)) + 1
    runtime = prepare_task_runtime(project_root, run_id, task, runtime=runtime)
    repaired_paths = set(apply_run_state_repair_preflight(project_root, run_id, task, runtime, repair_context))
    working_directory, task_sandbox_mode, prompt_metadata, execution_prompt = build_task_execution_materials(
        project_root,
        run_id,
        task_path,
        task,
        sandbox_mode=sandbox_mode,
        runtime=runtime,
    )
    prompt_observability = prompt_metrics(execution_prompt)
    execution_dir = ensure_dir(run_dir / "executions")
    task_runtime_contract = load_task_runtime_contract(
        run_dir,
        phase=str(task.get("phase") or ""),
        objective_id=str(task.get("objective_id") or ""),
        capability=str(task.get("capability") or ""),
        task_id=task_id,
    )
    runtime_requirements = (
        dict(task_runtime_contract.get("runtime_requirements", {}))
        if isinstance(task_runtime_contract, dict) and isinstance(task_runtime_contract.get("runtime_requirements"), dict)
        else infer_task_runtime_requirements(task)
    )
    output_schema_path = project_root / "orchestrator" / "schemas" / "executor-response.v1.json"
    last_message_path = execution_dir / f"{task_id}.last-message.json"
    stdout_path = execution_dir / f"{task_id}.stdout.jsonl"
    stderr_path = execution_dir / f"{task_id}.stderr.log"
    summary_path = execution_dir / f"{task_id}.json"
    report_path = run_dir / "reports" / f"{task_id}.json"
    clear_text(stdout_path)
    clear_text(stderr_path)
    clear_text(last_message_path)
    command = build_codex_command(
        codex_path=codex_path,
        working_directory=working_directory,
        output_schema_path=output_schema_path,
        last_message_path=last_message_path,
        sandbox_mode=task_sandbox_mode,
        additional_directories=task.get("additional_directories", []),
    )
    temp_root = task_temp_root(
        run_dir,
        task_id=task_id,
        attempt=runtime.attempt,
        runtime_requirements=runtime_requirements,
    )
    activity_state = ensure_activity(
        project_root,
        run_id,
        activity_id=task_id,
        kind="task_execution",
        entity_id=task_id,
        phase=task["phase"],
        objective_id=task["objective_id"],
        display_name=task_id,
        assigned_role=task["assigned_role"],
        status="prompt_rendered",
        progress_stage="prompt_rendered",
        current_activity="Rendered task prompt.",
        prompt_path=prompt_metadata["prompt_path"],
        stdout_path=str(stdout_path.relative_to(project_root)),
        stderr_path=str(stderr_path.relative_to(project_root)),
        output_path=str(report_path.relative_to(project_root)),
        runner_id="codex",
        observability=prompt_observability,
        warnings=list(runtime.runtime_warnings),
        parallel_execution_requested=runtime.parallel_execution_requested,
        parallel_execution_granted=runtime.parallel_execution_granted,
        parallel_fallback_reason=runtime.parallel_fallback_reason,
        workspace_path=runtime.workspace_path,
        branch_name=runtime.branch_name,
        attempt=runtime.attempt,
        recovery_action=runtime.recovery_action,
        begin_attempt=previous_activity is not None,
    )
    if repaired_paths:
        record_event(
            project_root,
            run_id,
            phase=task["phase"],
            activity_id=task_id,
            event_type="task.run_state_repair_applied",
            message=f"Patched stale run-local files before retrying task {task_id}.",
            payload={"paths": sorted(repaired_paths)},
        )
    for warning in runtime.runtime_warnings:
        append_activity_warning(
            project_root,
            run_id,
            task_id,
            code=warning["code"],
            message=warning["message"],
        )
    record_event(
        project_root,
        run_id,
        phase=task["phase"],
        activity_id=task_id,
        event_type="task.prompt_rendered",
        message=f"Rendered prompt for task {task_id}.",
        payload={"prompt_path": prompt_metadata["prompt_path"]},
    )
    update_activity(
        project_root,
        run_id,
        task_id,
        status="launching",
        progress_stage="launching",
        current_activity="Launching Codex worker.",
        queue_position=None,
        dependency_blockers=[],
        warnings=list(runtime.runtime_warnings),
        parallel_execution_requested=runtime.parallel_execution_requested,
        parallel_execution_granted=runtime.parallel_execution_granted,
        parallel_fallback_reason=runtime.parallel_fallback_reason,
        workspace_path=runtime.workspace_path,
        branch_name=runtime.branch_name,
    )
    record_event(
        project_root,
        run_id,
        phase=task["phase"],
        activity_id=task_id,
        event_type="task.launching",
        message=f"Launching task {task_id}.",
        payload={
            "command": command[:4],
            "workspace_path": runtime.workspace_path,
            "branch_name": runtime.branch_name,
        },
    )

    stdout_attempts: list[str] = []
    stderr_attempts: list[str] = []
    missing_final_message_retry_used = False
    stall_retry_used = False
    total_attempts = timeout_policy.max_timeout_retries + 1
    missing_final_message_attempt = 0
    stall_timeout_seconds = task_stall_timeout_seconds(timeout_policy.timeout_seconds)
    task_progress = {
        "last_stream_activity_at_monotonic": None,
        "thread_started_at_monotonic": None,
        "turn_started_at_monotonic": None,
        "process_started_at_monotonic": None,
    }
    final_response: str | None = None

    def on_stdout_line(raw_line: str) -> None:
        task_progress["last_stream_activity_at_monotonic"] = time.monotonic()
        stripped_line = raw_line.strip()
        if stripped_line:
            try:
                event_payload = json.loads(stripped_line)
            except json.JSONDecodeError:
                event_payload = None
            if isinstance(event_payload, dict):
                activity_at = task_progress["last_stream_activity_at_monotonic"]
                event_type = event_payload.get("type")
                if event_type == "thread.started":
                    task_progress["thread_started_at_monotonic"] = activity_at
                elif event_type == "turn.started":
                    task_progress["turn_started_at_monotonic"] = activity_at
        append_text(stdout_path, raw_line + "\n")
        note_activity_stream(
            project_root,
            run_id,
            task_id,
            stdout_bytes=len((raw_line + "\n").encode("utf-8")),
        )
        handle_codex_event_line(project_root, run_id, task["phase"], task_id, raw_line)

    def on_stderr_line(raw_line: str) -> None:
        task_progress["last_stream_activity_at_monotonic"] = time.monotonic()
        append_text(stderr_path, raw_line)
        note_activity_stream(
            project_root,
            run_id,
            task_id,
            stderr_bytes=len(raw_line.encode("utf-8")),
        )

    def on_process_started(process: subprocess.Popen[str]) -> None:
        task_progress["process_started_at_monotonic"] = time.monotonic()
        update_activity(
            project_root,
            run_id,
            task_id,
            process_metadata={
                "pid": process.pid,
                "started_at": activity_state["updated_at"],
                "command": " ".join(command),
                "cwd": str(working_directory),
            },
        )

    def stall_reason() -> str | None:
        last_activity_at = task_progress["last_stream_activity_at_monotonic"]
        now_monotonic = time.monotonic()
        turn_started_at = task_progress["turn_started_at_monotonic"]
        thread_started_at = task_progress["thread_started_at_monotonic"]
        process_started_at = task_progress["process_started_at_monotonic"]
        if turn_started_at is not None and last_activity_at is not None and now_monotonic - last_activity_at >= stall_timeout_seconds:
            return "stall_after_turn_started"
        if thread_started_at is not None and last_activity_at is not None and now_monotonic - last_activity_at >= stall_timeout_seconds:
            return "stall_after_thread_started"
        if process_started_at is not None and last_activity_at is None and now_monotonic - process_started_at >= stall_timeout_seconds:
            return "stall_before_first_output"
        return None

    while final_response is None:
        completed: CodexProcessResult | None = None
        call_started_at = now_timestamp()
        call_completed_at = now_timestamp()
        call_latency_ms = 0
        for timeout_attempt in range(1, total_attempts + 1):
            clear_text(last_message_path)
            call_started_at = now_timestamp()
            call_started_monotonic = time.monotonic()
            try:
                completed = run_codex_command(
                    command,
                    prompt=execution_prompt,
                    cwd=project_root,
                    env=build_exec_environment(temp_root),
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
                current_activity = read_activity(project_root, run_id, task_id)
                queue_wait_ms = int((current_activity.get("observability", {}) or {}).get("queue_wait_ms", 0))
                stdout_bytes = len(stall_stdout.encode("utf-8"))
                stderr_bytes = len(stall_stderr.encode("utf-8"))
                record_llm_call(
                    project_root,
                    run_id,
                    phase=task["phase"],
                    activity_id=task_id,
                    kind="task_execution",
                    attempt=runtime.attempt,
                    started_at=call_started_at,
                    completed_at=call_completed_at,
                    latency_ms=call_latency_ms,
                    queue_wait_ms=queue_wait_ms,
                    prompt_char_count=prompt_observability["prompt_char_count"],
                    prompt_line_count=prompt_observability["prompt_line_count"],
                    prompt_bytes=prompt_observability["prompt_bytes"],
                    timed_out=False,
                    retry_scheduled=timeout_attempt <= timeout_policy.max_timeout_retries,
                    success=False,
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    stdout_bytes=stdout_bytes,
                    stderr_bytes=stderr_bytes,
                    timeout_seconds=timeout_policy.timeout_seconds,
                    error=exc.reason,
                    label=task_id,
                )
                update_activity(
                    project_root,
                    run_id,
                    task_id,
                    observability=accumulate_observability(
                        current_activity["observability"],
                        latency_ms=call_latency_ms,
                        stdout_bytes=stdout_bytes,
                        stderr_bytes=stderr_bytes,
                        timed_out=False,
                        timeout_retry_scheduled=False,
                    ),
                )
                record_event(
                    project_root,
                    run_id,
                    phase=task["phase"],
                    activity_id=task_id,
                    event_type="task.stall_detected",
                    message=f"Task {task_id} stalled during execution.",
                    payload={
                        "reason": exc.reason,
                        "stall_timeout_seconds": exc.stall_seconds,
                    },
                )
                inferred_repair_context = infer_attempt_run_state_repair_context(
                    task,
                    parse_jsonl_events(stall_stdout),
                    existing_repair_context=repair_context,
                    trigger_reason=exc.reason,
                )
                if inferred_repair_context is not None:
                    repair_context = inferred_repair_context
                    task = apply_repair_context_to_task(task, repair_context)
                    runtime_requirements = infer_task_runtime_requirements(task)
                    task["execution_mode"] = resolve_task_execution_mode(task, runtime_requirements)
                    write_json(task_path, task)
                    repaired_paths.update(
                        apply_run_state_repair_preflight(project_root, run_id, task, runtime, repair_context)
                    )
                    working_directory, task_sandbox_mode, prompt_metadata, execution_prompt = build_task_execution_materials(
                        project_root,
                        run_id,
                        task_path,
                        task,
                        sandbox_mode=sandbox_mode,
                        runtime=runtime,
                    )
                    prompt_observability = prompt_metrics(execution_prompt)
                    command = build_codex_command(
                        codex_path=codex_path,
                        working_directory=working_directory,
                        output_schema_path=output_schema_path,
                        last_message_path=last_message_path,
                        sandbox_mode=task_sandbox_mode,
                        additional_directories=task.get("additional_directories", []),
                    )
                    temp_root = task_temp_root(
                        run_dir,
                        task_id=task_id,
                        attempt=runtime.attempt,
                        runtime_requirements=runtime_requirements,
                    )
                    record_event(
                        project_root,
                        run_id,
                        phase=task["phase"],
                        activity_id=task_id,
                        event_type="task.run_state_repair_applied",
                        message=f"Expanded task {task_id} into run-state repair before retrying after stall.",
                        payload={"paths": sorted(repaired_paths), "trigger_reason": exc.reason},
                    )
                if timeout_attempt <= timeout_policy.max_timeout_retries:
                    stall_retry_used = True
                    message = (
                        f"Task {task_id} stalled after {exc.stall_seconds} seconds "
                        f"({exc.reason}); retrying ({timeout_attempt}/{total_attempts})."
                    )
                    update_activity(
                        project_root,
                        run_id,
                        task_id,
                        status="recovering",
                        progress_stage="recovering",
                        current_activity=message,
                        status_reason="stall_retry_scheduled",
                        warnings=list(runtime.runtime_warnings),
                        parallel_execution_requested=runtime.parallel_execution_requested,
                        parallel_execution_granted=runtime.parallel_execution_granted,
                        parallel_fallback_reason=runtime.parallel_fallback_reason,
                        workspace_path=runtime.workspace_path,
                        branch_name=runtime.branch_name,
                        process_metadata=None,
                    )
                    record_event(
                        project_root,
                        run_id,
                        phase=task["phase"],
                        activity_id=task_id,
                        event_type="task.retry_scheduled",
                        message=message,
                        payload={
                            "reason": exc.reason,
                            "stall_timeout_seconds": exc.stall_seconds,
                            "attempt": timeout_attempt,
                            "max_attempts": total_attempts,
                        },
                    )
                    continue
                message = (
                    f"codex exec stalled after {exc.stall_seconds} seconds for task {task_id} "
                    f"({exc.reason}); retry-activity is recommended."
                )
                update_activity(
                    project_root,
                    run_id,
                    task_id,
                    status="failed",
                    progress_stage="failed",
                    current_activity=message,
                    status_reason=exc.reason,
                    warnings=list(runtime.runtime_warnings),
                    parallel_execution_requested=runtime.parallel_execution_requested,
                    parallel_execution_granted=runtime.parallel_execution_granted,
                    parallel_fallback_reason=runtime.parallel_fallback_reason,
                    workspace_path=runtime.workspace_path,
                    branch_name=runtime.branch_name,
                    process_metadata=None,
                )
                record_event(
                    project_root,
                    run_id,
                    phase=task["phase"],
                    activity_id=task_id,
                    event_type="task.failed",
                    message=message,
                    payload={
                        "reason": exc.reason,
                        "stall_timeout_seconds": exc.stall_seconds,
                    },
                )
                raise ExecutorError(message) from exc
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
                current_activity = read_activity(project_root, run_id, task_id)
                queue_wait_ms = int((current_activity.get("observability", {}) or {}).get("queue_wait_ms", 0))
                stdout_bytes = len(timeout_stdout.encode("utf-8"))
                stderr_bytes = len(timeout_stderr.encode("utf-8"))
                record_llm_call(
                    project_root,
                    run_id,
                    phase=task["phase"],
                    activity_id=task_id,
                    kind="task_execution",
                    attempt=runtime.attempt,
                    started_at=call_started_at,
                    completed_at=call_completed_at,
                    latency_ms=call_latency_ms,
                    queue_wait_ms=queue_wait_ms,
                    prompt_char_count=prompt_observability["prompt_char_count"],
                    prompt_line_count=prompt_observability["prompt_line_count"],
                    prompt_bytes=prompt_observability["prompt_bytes"],
                    timed_out=True,
                    retry_scheduled=timeout_attempt <= timeout_policy.max_timeout_retries,
                    success=False,
                    input_tokens=0,
                    cached_input_tokens=0,
                    output_tokens=0,
                    stdout_bytes=stdout_bytes,
                    stderr_bytes=stderr_bytes,
                    timeout_seconds=timeout_policy.timeout_seconds,
                    error="timeout",
                    label=task_id,
                )
                update_activity(
                    project_root,
                    run_id,
                    task_id,
                    observability=accumulate_observability(
                        current_activity["observability"],
                        latency_ms=call_latency_ms,
                        stdout_bytes=stdout_bytes,
                        stderr_bytes=stderr_bytes,
                        timed_out=True,
                        timeout_retry_scheduled=timeout_attempt <= timeout_policy.max_timeout_retries,
                    ),
                )
                if timeout_attempt <= timeout_policy.max_timeout_retries:
                    message = timeout_retry_message(
                        "task",
                        task_id,
                        timeout_seconds=timeout_policy.timeout_seconds,
                        attempt=timeout_attempt,
                        max_attempts=total_attempts,
                    )
                    update_activity(
                        project_root,
                        run_id,
                        task_id,
                        status="recovering",
                        progress_stage="recovering",
                        current_activity=message,
                        status_reason="timeout_retry_scheduled",
                        warnings=list(runtime.runtime_warnings),
                        parallel_execution_requested=runtime.parallel_execution_requested,
                        parallel_execution_granted=runtime.parallel_execution_granted,
                        parallel_fallback_reason=runtime.parallel_fallback_reason,
                        workspace_path=runtime.workspace_path,
                        branch_name=runtime.branch_name,
                        process_metadata=None,
                    )
                    record_event(
                        project_root,
                        run_id,
                        phase=task["phase"],
                        activity_id=task_id,
                        event_type="task.timeout_retry_scheduled",
                        message=message,
                        payload={
                            "timeout_seconds": timeout_policy.timeout_seconds,
                            "attempt": timeout_attempt,
                            "max_attempts": total_attempts,
                        },
                    )
                    inferred_repair_context = infer_attempt_run_state_repair_context(
                        task,
                        parse_jsonl_events(timeout_stdout),
                        existing_repair_context=repair_context,
                        trigger_reason="timeout_exhausted",
                    )
                    if inferred_repair_context is not None:
                        repair_context = inferred_repair_context
                        task = apply_repair_context_to_task(task, repair_context)
                        runtime_requirements = infer_task_runtime_requirements(task)
                        task["execution_mode"] = resolve_task_execution_mode(task, runtime_requirements)
                        write_json(task_path, task)
                        repaired_paths.update(
                            apply_run_state_repair_preflight(project_root, run_id, task, runtime, repair_context)
                        )
                        working_directory, task_sandbox_mode, prompt_metadata, execution_prompt = build_task_execution_materials(
                            project_root,
                            run_id,
                            task_path,
                            task,
                            sandbox_mode=sandbox_mode,
                            runtime=runtime,
                        )
                        prompt_observability = prompt_metrics(execution_prompt)
                        command = build_codex_command(
                            codex_path=codex_path,
                            working_directory=working_directory,
                            output_schema_path=output_schema_path,
                            last_message_path=last_message_path,
                            sandbox_mode=task_sandbox_mode,
                            additional_directories=task.get("additional_directories", []),
                        )
                        temp_root = task_temp_root(
                            run_dir,
                            task_id=task_id,
                            attempt=runtime.attempt,
                            runtime_requirements=runtime_requirements,
                        )
                        record_event(
                            project_root,
                            run_id,
                            phase=task["phase"],
                            activity_id=task_id,
                            event_type="task.run_state_repair_applied",
                            message=f"Expanded task {task_id} into run-state repair before retrying after timeout.",
                            payload={"paths": sorted(repaired_paths), "trigger_reason": "timeout_exhausted"},
                        )
                    continue
                failure_message = timeout_final_message(
                    "task",
                    task_id,
                    timeout_seconds=timeout_policy.timeout_seconds,
                    attempts=total_attempts,
                    resume_recommended=task.get("execution_mode", "read_only") == "isolated_write",
                    explicit_override=timeout_policy.source == "explicit",
                )
                update_activity(
                    project_root,
                    run_id,
                    task_id,
                    status="failed",
                    progress_stage="failed",
                    current_activity=failure_message,
                    status_reason="timeout_exhausted",
                    warnings=list(runtime.runtime_warnings),
                    parallel_execution_requested=runtime.parallel_execution_requested,
                    parallel_execution_granted=runtime.parallel_execution_granted,
                    parallel_fallback_reason=runtime.parallel_fallback_reason,
                    workspace_path=runtime.workspace_path,
                    branch_name=runtime.branch_name,
                    process_metadata=None,
                )
                record_event(
                    project_root,
                    run_id,
                    phase=task["phase"],
                    activity_id=task_id,
                    event_type="task.failed",
                    message=failure_message,
                    payload={"timeout_seconds": timeout_policy.timeout_seconds, "attempts": total_attempts},
                )
                raise ExecutorError(failure_message) from exc
        assert completed is not None
        stdout_attempts.append(completed.stdout)
        stderr_attempts.append(completed.stderr)

        events = parse_jsonl_events(completed.stdout)
        usage = extract_usage(events) or {}
        final_response_candidate = extract_final_response_with_fallback(
            events,
            last_message_path=last_message_path,
        )
        current_activity = read_activity(project_root, run_id, task_id)
        queue_wait_ms = int((current_activity.get("observability", {}) or {}).get("queue_wait_ms", 0))
        stdout_bytes = len(completed.stdout.encode("utf-8"))
        stderr_bytes = len(completed.stderr.encode("utf-8"))
        failure = extract_turn_failure(events)
        llm_error = failure or ("missing_final_agent_message" if final_response_candidate is None else None)
        record_llm_call(
            project_root,
            run_id,
            phase=task["phase"],
            activity_id=task_id,
            kind="task_execution",
            attempt=runtime.attempt,
            started_at=call_started_at,
            completed_at=call_completed_at,
            latency_ms=call_latency_ms,
            queue_wait_ms=queue_wait_ms,
            prompt_char_count=prompt_observability["prompt_char_count"],
            prompt_line_count=prompt_observability["prompt_line_count"],
            prompt_bytes=prompt_observability["prompt_bytes"],
            timed_out=False,
            retry_scheduled=False,
            success=completed.returncode == 0 and llm_error is None,
            input_tokens=int(usage.get("input_tokens", 0)),
            cached_input_tokens=int(usage.get("cached_input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            timeout_seconds=timeout_policy.timeout_seconds,
            error=llm_error,
            label=task_id,
        )
        update_activity(
            project_root,
            run_id,
            task_id,
            observability=accumulate_observability(
                current_activity["observability"],
                latency_ms=call_latency_ms,
                input_tokens=int(usage.get("input_tokens", 0)),
                cached_input_tokens=int(usage.get("cached_input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)),
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                timed_out=False,
                timeout_retry_scheduled=False,
            ),
        )
        if completed.returncode != 0 or failure is not None:
            message = failure or completed.stderr.strip() or f"codex exec exited with code {completed.returncode}"
            update_activity(
                project_root,
                run_id,
                task_id,
                status="failed",
                progress_stage="failed",
                current_activity=message,
                warnings=list(runtime.runtime_warnings),
                parallel_execution_requested=runtime.parallel_execution_requested,
                parallel_execution_granted=runtime.parallel_execution_granted,
                parallel_fallback_reason=runtime.parallel_fallback_reason,
                workspace_path=runtime.workspace_path,
                branch_name=runtime.branch_name,
                process_metadata=None,
            )
            record_event(
                project_root,
                run_id,
                phase=task["phase"],
                activity_id=task_id,
                event_type="task.failed",
                message=f"Task {task_id} failed.",
                payload={"error": message},
            )
            raise ExecutorError(message)
        if final_response_candidate is None:
            missing_final_message_attempt += 1
            if missing_final_message_attempt <= MAX_TASK_EXECUTION_MISSING_FINAL_MESSAGE_RETRIES:
                missing_final_message_retry_used = True
                message = (
                    f"Task {task_id} produced no final agent message; retrying "
                    f"({missing_final_message_attempt}/{MAX_TASK_EXECUTION_MISSING_FINAL_MESSAGE_RETRIES + 1})."
                )
                update_activity(
                    project_root,
                    run_id,
                    task_id,
                    status="recovering",
                    progress_stage="recovering",
                    current_activity=message,
                    status_reason="missing_final_message_retry_scheduled",
                    warnings=list(runtime.runtime_warnings),
                    parallel_execution_requested=runtime.parallel_execution_requested,
                    parallel_execution_granted=runtime.parallel_execution_granted,
                    parallel_fallback_reason=runtime.parallel_fallback_reason,
                    workspace_path=runtime.workspace_path,
                    branch_name=runtime.branch_name,
                    process_metadata=None,
                )
                record_event(
                    project_root,
                    run_id,
                    phase=task["phase"],
                    activity_id=task_id,
                    event_type="task.retry_scheduled",
                    message=message,
                    payload={
                        "reason": "missing_final_agent_message",
                        "attempt": missing_final_message_attempt,
                        "max_attempts": MAX_TASK_EXECUTION_MISSING_FINAL_MESSAGE_RETRIES + 1,
                    },
                )
                continue
            message = "No final agent message was found in codex exec output"
            update_activity(
                project_root,
                run_id,
                task_id,
                status="failed",
                progress_stage="failed",
                current_activity=message,
                status_reason="missing_final_agent_message",
                warnings=list(runtime.runtime_warnings),
                parallel_execution_requested=runtime.parallel_execution_requested,
                parallel_execution_granted=runtime.parallel_execution_granted,
                parallel_fallback_reason=runtime.parallel_fallback_reason,
                workspace_path=runtime.workspace_path,
                branch_name=runtime.branch_name,
                process_metadata=None,
            )
            record_event(
                project_root,
                run_id,
                phase=task["phase"],
                activity_id=task_id,
                event_type="task.failed",
                message=f"Task {task_id} failed during execution.",
                payload={"error": message, "reason": "missing_final_agent_message"},
            )
            raise ExecutorError(message)
        final_response = final_response_candidate
    try:
        parsed_response = json.loads(final_response)
    except json.JSONDecodeError as exc:
        update_activity(
            project_root,
            run_id,
            task_id,
            status="failed",
            progress_stage="failed",
            current_activity="Final response was not valid JSON.",
            warnings=list(runtime.runtime_warnings),
            parallel_execution_requested=runtime.parallel_execution_requested,
            parallel_execution_granted=runtime.parallel_execution_granted,
            parallel_fallback_reason=runtime.parallel_fallback_reason,
            workspace_path=runtime.workspace_path,
            branch_name=runtime.branch_name,
            process_metadata=None,
        )
        raise ExecutorError(f"Final response was not valid JSON: {final_response}") from exc

    parsed_response = normalize_executor_response_payload(parsed_response)
    try:
        validate_document(parsed_response, "executor-response.v1", project_root)
    except SchemaValidationError as exc:
        update_activity(
            project_root,
            run_id,
            task_id,
            status="failed",
            progress_stage="failed",
            current_activity="Executor response failed schema validation.",
            warnings=list(runtime.runtime_warnings),
            parallel_execution_requested=runtime.parallel_execution_requested,
            parallel_execution_granted=runtime.parallel_execution_granted,
            parallel_fallback_reason=runtime.parallel_fallback_reason,
            workspace_path=runtime.workspace_path,
            branch_name=runtime.branch_name,
        )
        raise ExecutorError(f"Executor response failed schema validation: {exc}") from exc

    report, collaboration_ids, change_request_ids = materialize_executor_response(
        project_root,
        run_id,
        task,
        parsed_response,
        runtime_warnings=runtime.runtime_warnings,
        runtime_recovery={
            "attempt": runtime.attempt,
            "recovery_action": runtime.recovery_action
            or (
                "missing_final_message_retry"
                if missing_final_message_retry_used
                else (
                    "stall_retry"
                    if stall_retry_used
                    else ("timeout_retry" if len(stdout_attempts) > 1 else None)
                )
            ),
            "workspace_reused": runtime.workspace_reused,
            "timeout_retries_used": max(0, len(stdout_attempts) - 1),
        },
        runtime_observability=compact_observability_for_report(
            read_activity(project_root, run_id, task_id)["observability"]
        ),
    )
    if task.get("execution_mode", "read_only") == "isolated_write" and report["status"] == "ready_for_bundle_review":
        commit_result = commit_isolated_workspace(runtime, task)
        runtime.commit_sha = commit_result.get("commit_sha")
        if commit_result.get("discarded_paths"):
            append_activity_warning(
                project_root,
                run_id,
                task_id,
                code="discarded_paths",
                message=(
                    "Discarded incidental workspace changes outside the task contract: "
                    + ", ".join(commit_result["discarded_paths"])
                ),
            )
            record_event(
                project_root,
                run_id,
                phase=task["phase"],
                activity_id=task_id,
                event_type="task.workspace_sanitized",
                message=f"Task {task_id} discarded incidental workspace changes before commit.",
                payload={"discarded_paths": commit_result["discarded_paths"]},
            )
    recovery_action = runtime.recovery_action or (
        "missing_final_message_retry"
        if missing_final_message_retry_used
        else (
            "stall_retry"
            if stall_retry_used
            else ("timeout_retry" if len(stdout_attempts) > 1 else None)
        )
    )
    activity_status = "recovered" if runtime.attempt > 1 or recovery_action else report["status"]
    update_activity(
        project_root,
        run_id,
        task_id,
        status=activity_status,
        progress_stage=activity_status,
        current_activity=report["summary"],
        output_path=str(report_path.relative_to(project_root)),
        queue_position=None,
        dependency_blockers=[],
        warnings=list(runtime.runtime_warnings),
        parallel_execution_requested=runtime.parallel_execution_requested,
        parallel_execution_granted=runtime.parallel_execution_granted,
        parallel_fallback_reason=runtime.parallel_fallback_reason,
        workspace_path=runtime.workspace_path,
        branch_name=runtime.branch_name,
        process_metadata=None,
        recovered_at=now_timestamp() if activity_status == "recovered" else None,
        recovery_action=recovery_action,
    )
    for change_request in report.get("change_requests", []):
        record_event(
            project_root,
            run_id,
            phase=task["phase"],
            activity_id=task_id,
            event_type="task.change_request_created",
            message=f"Task {task_id} created change request {change_request['change_id']}.",
            payload={
                "change_id": change_request["change_id"],
                "change_category": change_request["change_category"],
                "approval": change_request["approval"],
            },
        )
    record_event(
        project_root,
        run_id,
        phase=task["phase"],
        activity_id=task_id,
        event_type="task.completed",
        message=f"Task {task_id} finished with status {report['status']}.",
        payload={
            "status": report["status"],
            "report_path": str(report_path.relative_to(project_root)),
            "change_request_ids": change_request_ids,
            "attempt": runtime.attempt,
            "parallel_execution_requested": runtime.parallel_execution_requested,
            "parallel_execution_granted": runtime.parallel_execution_granted,
            "parallel_fallback_reason": runtime.parallel_fallback_reason,
            "branch_name": runtime.branch_name,
            "workspace_path": runtime.workspace_path,
            "commit_sha": runtime.commit_sha,
            "recovery_action": recovery_action,
            "workspace_reused": runtime.workspace_reused,
        },
    )
    execution_summary = {
        "task_id": task_id,
        "thread_id": extract_thread_id(events),
        "usage": usage or None,
        "stdout_path": str(stdout_path.relative_to(project_root)),
        "stderr_path": str(stderr_path.relative_to(project_root)),
        "last_message_path": str(last_message_path.relative_to(project_root)),
        "report_path": str(report_path.relative_to(project_root)),
        "collaboration_request_ids": collaboration_ids,
        "change_request_ids": change_request_ids,
        "status": report["status"],
        "runtime_warnings": list(runtime.runtime_warnings),
        "parallel_execution_requested": runtime.parallel_execution_requested,
        "parallel_execution_granted": runtime.parallel_execution_granted,
        "parallel_fallback_reason": runtime.parallel_fallback_reason,
        "branch_name": runtime.branch_name,
        "workspace_path": runtime.workspace_path,
        "commit_sha": runtime.commit_sha,
        "attempt": runtime.attempt,
        "recovery_action": runtime.recovery_action,
        "workspace_reused": runtime.workspace_reused,
        "runtime_observability": read_activity(project_root, run_id, task_id)["observability"],
    }
    write_json(summary_path, execution_summary)
    if change_request_ids:
        apply_approved_change_impacts(project_root, run_id, change_request_ids)
    return execution_summary


def prepare_task_runtime(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    *,
    runtime: TaskExecutionRuntime | None = None,
) -> TaskExecutionRuntime:
    resolved = runtime or TaskExecutionRuntime()
    if not task_needs_workspace_snapshot(project_root, run_id, task):
        return resolved
    existing_workspace = task_workspace_exists(project_root, run_id, task["task_id"])
    refresh_workspace = existing_workspace and resolved.attempt > 1
    try:
        workspace = ensure_task_workspace_with_refresh(
            project_root,
            run_id,
            task["task_id"],
            refresh=refresh_workspace,
        )
    except WorktreeError as exc:
        raise ExecutorError(str(exc)) from exc
    resolved.branch_name = workspace.branch_name
    resolved.workspace_path = str(workspace.workspace_path)
    resolved.workspace_reused = existing_workspace and not refresh_workspace
    if refresh_workspace:
        resolved.recovery_action = resolved.recovery_action or "refreshed_workspace"
    elif existing_workspace:
        resolved.recovery_action = resolved.recovery_action or "reused_workspace"
    elif resolved.attempt > 1:
        resolved.recovery_action = resolved.recovery_action or "recreated_workspace"
    return resolved


def task_needs_workspace_snapshot(project_root: Path, run_id: str, task: dict[str, Any]) -> bool:
    if task.get("execution_mode", "read_only") == "isolated_write":
        return True
    if referenced_task_output_ids(task):
        return True
    if any(isinstance(value, str) and value.strip() for value in task.get("handoff_dependencies", [])):
        return True
    if task_declares_resolved_workspace_file_inputs(project_root, run_id, task):
        return True
    return task_declares_workspace_file_inputs(task)


def task_declares_workspace_file_inputs(task: dict[str, Any]) -> bool:
    for raw_input in task.get("inputs", []):
        if not isinstance(raw_input, str):
            continue
        normalized = raw_input.strip()
        if not normalized:
            continue
        if normalized.startswith(("Planning Inputs.", "Runtime Context.", "Output of ", "Outputs from ")):
            continue
        return True
    return False


def task_declares_resolved_workspace_file_inputs(project_root: Path, run_id: str, task: dict[str, Any]) -> bool:
    resolved_inputs = preview_resolved_inputs(
        project_root,
        run_id,
        task,
        sandbox_mode=task.get("sandbox_mode"),
    )
    return bool(collect_resolved_input_file_paths(resolved_inputs))


def materialize_task_context_files(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    working_directory: Path,
) -> list[str]:
    if not working_directory.exists():
        return []
    if working_directory.resolve() == project_root.resolve():
        return []
    materialized: set[str] = set()
    ensure_declared_output_parent_directories(task, working_directory)
    materialized.update(
        mirror_explicit_input_files_into_workspace(project_root, run_id, task, working_directory)
    )
    materialized.update(
        mirror_resolved_input_files_into_workspace(project_root, run_id, task, working_directory)
    )
    run_dir = project_root / "runs" / run_id
    source_task_ids = referenced_task_output_ids(task)
    for handoff_id in [value for value in task.get("handoff_dependencies", []) if isinstance(value, str)]:
        handoff_path = run_dir / "collaboration-plans" / f"{handoff_id}.json"
        if not handoff_path.exists():
            continue
        handoff = read_json(handoff_path)
        source_task_id = handoff.get("from_task_id")
        if isinstance(source_task_id, str) and source_task_id:
            source_task_ids.add(source_task_id)
        for artifact_path, source_path in collect_handoff_artifact_paths(project_root, handoff).items():
            destination = (working_directory / artifact_path).resolve()
            ensure_dir(destination.parent)
            if destination == source_path.resolve():
                continue
            mirror_input_file_into_workspace(project_root, run_id, source_path, destination)
            materialized.add(str(destination.relative_to(working_directory)))
    for source_task_id in sorted(source_task_ids):
        report_path = run_dir / "reports" / f"{source_task_id}.json"
        if not report_path.exists():
            continue
        materialized.update(
            mirror_report_into_workspace(project_root, run_id, source_task_id, report_path, working_directory)
        )
    return sorted(materialized)


def workspace_relative_path(working_directory: Path, destination: Path) -> str:
    return str(destination.resolve().relative_to(working_directory.resolve()))


def ensure_declared_output_parent_directories(task: dict[str, Any], working_directory: Path) -> None:
    for descriptor in normalize_output_descriptors(list(task.get("expected_outputs", []))):
        output_path = descriptor.get("path")
        if not isinstance(output_path, str) or not output_path.strip():
            continue
        destination = (working_directory / output_path.strip()).resolve()
        ensure_dir(destination.parent)


def mirror_explicit_input_files_into_workspace(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    working_directory: Path,
) -> set[str]:
    mirrored: set[str] = set()
    materialized: set[str] = set()
    for raw_input in task.get("inputs", []):
        if not isinstance(raw_input, str):
            continue
        normalized = raw_input.strip()
        lowered = normalized.lower()
        if not normalized:
            continue
        if normalized.startswith("Runtime Context") or normalized.startswith("Planning Inputs"):
            continue
        if lowered.startswith("output of ") or lowered.startswith("outputs from "):
            continue
        input_path = Path(normalized)
        if input_path.is_absolute():
            continue
        source_path = resolve_workspace_input_path(project_root, run_id, normalized)
        if source_path is None:
            continue
        if not source_path.is_file():
            continue
        destination = (working_directory / input_path).resolve()
        destination_key = str(destination)
        if destination_key in mirrored:
            mirrored.add(destination_key)
            continue
        mirror_input_file_into_workspace(project_root, run_id, source_path, destination)
        mirrored.add(destination_key)
        materialized.add(workspace_relative_path(working_directory, destination))
    return materialized


def mirror_resolved_input_files_into_workspace(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    working_directory: Path,
) -> set[str]:
    resolved_inputs = preview_resolved_inputs(
        project_root,
        run_id,
        task,
        working_directory=working_directory,
        sandbox_mode=task.get("sandbox_mode"),
    )
    mirrored: set[str] = set()
    materialized: set[str] = set()
    for candidate_path in collect_resolved_input_file_paths(resolved_inputs):
        source_path = resolve_workspace_input_path(project_root, run_id, candidate_path)
        if source_path is None or not source_path.is_file():
            continue
        destination = (working_directory / candidate_path).resolve()
        destination_key = str(destination)
        if destination_key in mirrored:
            mirrored.add(destination_key)
            continue
        mirror_input_file_into_workspace(project_root, run_id, source_path, destination)
        mirrored.add(destination_key)
        materialized.add(workspace_relative_path(working_directory, destination))
    return materialized


def collect_resolved_input_file_paths(payload: Any) -> set[str]:
    paths: set[str] = set()

    def is_workspace_relative_path(value: str) -> bool:
        normalized = value.strip()
        if not normalized or "\n" in normalized or "\r" in normalized:
            return False
        if len(normalized) > 512:
            return False
        if normalized.startswith(("Planning Inputs", "Runtime Context", "Output of ", "Outputs from ")):
            return False
        if normalized.startswith(("{", "[")) or "://" in normalized:
            return False
        candidate = Path(normalized)
        if candidate.is_absolute():
            return False
        return "/" in normalized

    def visit(value: Any, *, key: str | None = None) -> None:
        if isinstance(value, str):
            normalized = value.strip()
            if (key == "path" or key is None) and is_workspace_relative_path(normalized):
                paths.add(normalized)
            return
        if isinstance(value, dict):
            for nested_key, nested in value.items():
                visit(nested, key=nested_key)
            return
        if isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    return paths


def referenced_task_output_ids(task: dict[str, Any]) -> set[str]:
    task_ids: set[str] = set()
    for raw_input in task.get("inputs", []):
        if not isinstance(raw_input, str):
            continue
        normalized = raw_input.strip()
        lowered = normalized.lower()
        if lowered.startswith("output of "):
            task_ids.add(normalized.split(" ", 2)[2].strip())
        elif lowered.startswith("outputs from "):
            task_ids.add(normalized.split(" ", 2)[2].strip())
    return {task_id for task_id in task_ids if task_id}


def mirror_report_into_workspace(
    project_root: Path,
    run_id: str,
    source_task_id: str,
    report_path: Path,
    working_directory: Path,
) -> set[str]:
    report = load_input_artifact_for_run(project_root, run_id, report_path)
    report_destination = working_directory / "runs" / run_id / "reports" / f"{source_task_id}.json"
    mirror_input_file_into_workspace(project_root, run_id, report_path, report_destination)
    materialized = {workspace_relative_path(working_directory, report_destination)}
    for artifact in report.get("artifacts", [])[:12]:
        artifact_path = artifact.get("path")
        if not isinstance(artifact_path, str) or not artifact_path.strip():
            continue
        source_path = resolve_report_artifact_path(project_root, run_id, source_task_id, artifact_path)
        if source_path is None or not source_path.is_file():
            continue
        destination = (working_directory / artifact_path).resolve()
        ensure_dir(destination.parent)
        if destination == source_path.resolve():
            continue
        mirror_input_file_into_workspace(project_root, run_id, source_path, destination)
        materialized.add(workspace_relative_path(working_directory, destination))
    return materialized


def mirror_input_file_into_workspace(
    project_root: Path,
    run_id: str,
    source_path: Path,
    destination: Path,
) -> None:
    ensure_dir(destination.parent)
    if source_path.suffix == ".json":
        write_json(destination, load_input_artifact_for_run(project_root, run_id, source_path))
        return
    shutil.copy2(source_path, destination)


def task_landing_paths(task: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for path_value in task.get("owned_paths", []):
        if isinstance(path_value, str) and path_value.strip() and path_value not in paths:
            paths.append(path_value)
    for path_value in task.get("writes_existing_paths", []):
        if isinstance(path_value, str) and path_value.strip() and path_value not in paths:
            paths.append(path_value)
    for descriptor in normalize_output_descriptors(list(task.get("expected_outputs", []))):
        path_value = descriptor.get("path")
        if isinstance(path_value, str) and path_value.strip() and path_value not in paths:
            paths.append(path_value)
    return paths


def commit_isolated_workspace(runtime: TaskExecutionRuntime, task: dict[str, Any]) -> dict[str, Any]:
    if not runtime.branch_name or not runtime.workspace_path:
        return {"committed": False, "commit_sha": None, "discarded_paths": []}
    try:
        return commit_task_workspace(
            WorkspaceInfo(branch_name=runtime.branch_name, workspace_path=Path(runtime.workspace_path)),
            task["task_id"],
            allowed_paths=task_landing_paths(task),
        )
    except WorktreeError as exc:
        raise ExecutorError(str(exc)) from exc


def task_temp_root(
    run_dir: Path,
    *,
    task_id: str,
    attempt: int,
    runtime_requirements: dict[str, Any] | None = None,
) -> Path:
    requirements = runtime_requirements or {}
    if requirements.get("requires_writable_temp"):
        return ensure_dir(run_dir / "scratch" / task_id / f"attempt-{max(1, int(attempt))}")
    return ensure_dir(run_dir / "scratch" / task_id / f"attempt-{max(1, int(attempt))}")


def resolve_task_execution_mode(task: dict[str, Any], runtime_requirements: dict[str, Any] | None = None) -> str:
    current_mode = str(task.get("execution_mode") or "read_only").strip() or "read_only"
    requirements = runtime_requirements or infer_task_runtime_requirements(task)
    if current_mode == "read_only" and (
        task_requires_write_access(task)
        or requirements.get("requires_writable_temp")
        or requirements.get("requires_writable_workspace")
    ):
        return "isolated_write"
    return current_mode


def build_exec_environment(temp_root: Path | None = None) -> dict[str, str]:
    env = dict(os.environ)
    # Force the CLI to rely on its existing ChatGPT login/session path.
    env.pop("CODEX_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    if temp_root is not None:
        temp_dir = ensure_dir(temp_root / ".codex-tmp")
        temp_dir_str = str(temp_dir)
        env["TMPDIR"] = temp_dir_str
        env["TMP"] = temp_dir_str
        env["TEMP"] = temp_dir_str
    return env


def configured_mcp_disable_overrides() -> list[str]:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config_path = codex_home / "config.toml"
    if not config_path.exists():
        return []
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    mcp_servers = config.get("mcp_servers")
    if not isinstance(mcp_servers, dict):
        return []
    overrides: list[str] = []
    for name, settings in sorted(mcp_servers.items()):
        if not isinstance(name, str) or not name.strip():
            continue
        enabled = True
        if isinstance(settings, dict) and "enabled" in settings:
            enabled = bool(settings["enabled"])
        if enabled:
            overrides.append(f"mcp_servers.{name}.enabled=false")
    return overrides


def build_codex_command(
    *,
    codex_path: str,
    working_directory: Path,
    output_schema_path: Path,
    last_message_path: Path,
    sandbox_mode: str,
    additional_directories: list[str],
) -> list[str]:
    command = [
        codex_path,
        "exec",
        "--json",
        "--ephemeral",
        "-s",
        sandbox_mode,
        "--output-schema",
        str(output_schema_path),
        "-o",
        str(last_message_path),
        "-C",
        str(working_directory),
    ]
    for override in configured_mcp_disable_overrides():
        command.extend(["-c", override])
    for directory in additional_directories:
        command.extend(["--add-dir", directory])
    return command


def build_execution_prompt(prompt_text: str) -> str:
    return (
        prompt_text
        + "\n\n# Execution Rules\n\n"
        + "Use the task prompt above as the source of truth for what work to do.\n"
        + "Use the rules below to decide how to carry out the task and how to format the result.\n\n"
        + "## How To Use Inputs During Execution\n\n"
        + "- Treat the injected `Resolved Inputs` as authoritative.\n"
        + "- If an upstream artifact, handoff payload, or contract is already present in the resolved inputs, use it directly.\n"
        + "- Do not re-discover the same upstream context by searching `runs/`, sibling task workspaces, or unrelated source trees.\n"
        + "- Only inspect the filesystem for an upstream artifact when the resolved inputs clearly show that the preview or payload is missing.\n"
        + "- Once the workspace root is known, use workspace-relative paths as the canonical form.\n"
        + "- Do not probe both relative and absolute versions of the same path.\n\n"
        + "## How To Use The Filesystem\n\n"
        + "- Do not waste turns on exploratory commands such as repeated `pwd`, `ls`, or directory listings when the task prompt already provides the workspace root, owned paths, expected outputs, and resolved inputs.\n"
        + "- Do not read unrelated manifests, READMEs, or source trees unless they are directly relevant to the assigned task.\n"
        + "- Avoid duplicate shell commands once a path or artifact has already been confirmed.\n"
        + "- If the task owns a new output path that does not exist yet, create its parent directory and write the artifact directly.\n"
        + "- For discovery and design artifact tasks, start by producing the declared outputs from the provided inputs instead of rediscovering the workspace layout.\n"
        + "- Do not re-read the generated prompt log or task prompt file from `runs/...` unless the task explicitly lists it as an input.\n"
        + "- For discovery or design producing tasks, do not run `test -f`, `rg`, or `grep` against files you just created merely to prove they exist or contain required headings.\n\n"
        + "## How To Handle Validation Failure\n\n"
        + "- Rerun a required validation only after making a concrete change intended to fix the observed failure.\n"
        + "- Do not repeat the same failing validation without a new fix.\n"
        + "- If you no longer have a concrete next fix, return a final `blocked` response instead of continuing to iterate.\n"
        + "- Always end the task with one final JSON response.\n\n"
        + "## How To Decide Task Status\n\n"
        + "Use `ready_for_bundle_review` only when:\n"
        + "- the task contract is satisfied\n"
        + "- required outputs are produced\n"
        + "- required validations passed\n"
        + "- no blocking issue remains\n\n"
        + "Use `blocked` when:\n"
        + "- another team, manager, or custodian must act before the task can continue\n"
        + "- required context is missing and no allowed local substitute exists\n"
        + "- required validation cannot pass because a true blocker is still unresolved\n\n"
        + "If any open issue still prevents completion, do not return `ready_for_bundle_review`.\n\n"
        + "## How To Report Blockers\n\n"
        + "Use `blockers` to report factual execution blockers only.\n\n"
        + "Use a blocker when:\n"
        + "- a required input, artifact, or dependency is missing\n"
        + "- the environment prevents a required validation or command from running\n"
        + "- injected contracts or handoffs conflict and local guessing would be unsafe\n"
        + "- another role or team owns the failing surface\n\n"
        + "Do not use `blockers` to propose replans, collaboration workflows, or new plan structures.\n"
        + "Keep blocker entries factual and concrete.\n\n"
        + "## How To Report Produced Outputs\n\n"
        + "- Use only declared `output_id` values\n"
        + "- Do not invent outputs\n"
        + "- For blocked responses, return an empty array\n\n"
        + "## How To Handle Contradictions\n\n"
        + "If injected design artifacts, runtime contracts, or handoff payloads contradict each other:\n"
        + "- stop at the contradiction\n"
        + "- report the exact conflicting paths or artifacts\n"
        + "- do not guess a merged contract\n\n"
        + "If a blocker comes from conflicting upstream inputs:\n"
        + "- cite the narrowest exact entries from input source metadata\n"
        + "- do not guess impact ids from local outputs\n\n"
        + "## Response Rules\n\n"
        + "- Return only one JSON object matching the output schema\n"
        + "- Do not wrap the JSON in markdown fences\n"
    )


def handle_codex_event_line(project_root: Path, run_id: str, phase: str, activity_id: str, raw_line: str) -> None:
    line = raw_line.strip()
    if not line:
        return
    observed_at = now_timestamp()
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        record_event(
            project_root,
            run_id,
            phase=phase,
            activity_id=activity_id,
            event_type="codex.stdout.raw",
            message=truncate_text(line, 160),
            payload={},
        )
        return
    if not isinstance(event, dict):
        return
    event_type, message, payload, activity_updates, timestamp_updates = normalize_codex_event(
        event,
        observed_at=observed_at,
    )
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=activity_id,
        event_type=event_type,
        message=message,
        payload=payload,
    )
    if timestamp_updates:
        update_activity_observability_timestamps(project_root, run_id, activity_id, **timestamp_updates)
    if activity_updates:
        update_activity(project_root, run_id, activity_id, **activity_updates)


def validate_compiled_task_context(task_context: dict[str, Any]) -> None:
    missing_inputs = list(task_context.get("missing_inputs", []))
    if not missing_inputs:
        return
    details = []
    for item in missing_inputs:
        input_ref = str(item.get("input_ref") or "").strip()
        reason = str(item.get("reason") or "").strip()
        detail = str(item.get("detail") or "").strip()
        if detail:
            details.append(f"{input_ref} ({reason}: {detail})")
        else:
            details.append(f"{input_ref} ({reason})")
    raise ExecutorError(
        "Task references inputs that were not compiled into concrete context: " + "; ".join(details)
    )


def normalize_codex_event(
    event: dict[str, Any],
    *,
    observed_at: str,
) -> tuple[str, str, dict[str, Any], dict[str, Any] | None, dict[str, str] | None]:
    event_type = str(event.get("type", "codex.unknown"))
    if event_type == "thread.started":
        thread_id = event.get("thread_id")
        return (
            "codex.thread.started",
            f"Codex thread started: {thread_id}",
            {"thread_id": thread_id},
            {"status": "launching", "progress_stage": "launching", "current_activity": "Codex thread started."},
            {"thread_started_at": observed_at, "first_stream_at": observed_at},
        )
    if event_type == "turn.started":
        return (
            "codex.turn.started",
            "Codex turn started.",
            {},
            {"status": "launching", "progress_stage": "launching", "current_activity": "Codex turn started."},
            {"turn_started_at": observed_at},
        )
    if event_type == "turn.completed":
        usage = event.get("usage", {})
        return (
            "codex.turn.completed",
            "Codex turn completed.",
            {"usage": usage if isinstance(usage, dict) else {}},
            {
                "status": "finalizing",
                "progress_stage": "finalizing",
                "current_activity": "Codex turn completed.",
            },
            {"turn_completed_at": observed_at},
        )
    if event_type == "turn.failed":
        error = event.get("error", {})
        message = "Codex turn failed."
        if isinstance(error, dict) and error.get("message"):
            message = f"Codex turn failed: {error['message']}"
        return (
            "codex.turn.failed",
            message,
            {"error": error if isinstance(error, dict) else {}},
            {"status": "failed", "progress_stage": "failed", "current_activity": message},
            None,
        )
    if event_type == "error":
        message = str(event.get("message", "Codex stream error."))
        if message.lower().startswith("reconnecting..."):
            return (
                "codex.error.transient",
                message,
                {"message": message},
                None,
                None,
            )
        return (
            "codex.error",
            message,
            {"message": message},
            {"status": "failed", "progress_stage": "failed", "current_activity": message},
            None,
        )

    if event_type in {"item.started", "item.completed"}:
        item = event.get("item", {})
        if not isinstance(item, dict):
            return (f"codex.{event_type}", f"Codex {event_type}.", {}, None, None)
        item_type = str(item.get("type", "unknown"))
        if item_type == "command_execution":
            command = truncate_text(str(item.get("command", "")), 160)
            status = "running"
            current = f"Running command: {command}" if event_type == "item.started" else f"Completed command: {command}"
            return (
                f"codex.{event_type}.command_execution",
                current,
                {
                    "command": command,
                    "exit_code": item.get("exit_code"),
                    "item_id": item.get("id"),
                },
                {"status": status, "progress_stage": status, "current_activity": current},
                None,
            )
        if item_type == "agent_message":
            text_preview = truncate_text(str(item.get("text", "")), 200)
            return (
                f"codex.{event_type}.agent_message",
                "Received final agent message.",
                {"item_id": item.get("id"), "text_preview": text_preview},
                {"status": "finalizing", "progress_stage": "finalizing", "current_activity": "Received final agent response."},
                None,
            )
        return (
            f"codex.{event_type}.{item_type}",
            f"Codex {event_type} for {item_type}.",
            {"item_id": item.get("id"), "item_type": item_type},
            None,
            None,
        )

    return (f"codex.{event_type}", f"Codex event {event_type}.", {}, None, None)


def parse_jsonl_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def extract_thread_id(events: list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") == "thread.started":
            return event.get("thread_id")
    return None


def extract_usage(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
            if isinstance(usage, dict):
                return usage
    return None


def extract_turn_failure(events: list[dict[str, Any]]) -> str | None:
    last_completed_index: int | None = None
    last_failed_index: int | None = None
    last_failed_message: str | None = None
    last_error_index: int | None = None
    last_error_message: str | None = None
    for index, event in enumerate(events):
        event_type = event.get("type")
        if event_type == "turn.completed":
            last_completed_index = index
            continue
        if event_type == "turn.failed":
            error = event.get("error", {})
            if isinstance(error, dict):
                last_failed_message = str(error.get("message", "turn failed"))
            else:
                last_failed_message = "turn failed"
            last_failed_index = index
            continue
        if event_type == "error":
            last_error_message = str(event.get("message", "stream error"))
            last_error_index = index
    if last_completed_index is not None and (
        last_failed_index is None or last_completed_index > last_failed_index
    ):
        return None
    if last_failed_index is not None:
        return last_failed_message or "turn failed"
    if last_error_index is not None:
        return last_error_message or "stream error"
    return None


def extract_final_response(events: list[dict[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("type") != "item.completed":
            continue
        item = event.get("item", {})
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                return text
    raise ExecutorError("No final agent message was found in codex exec output")


def load_final_response_from_path(last_message_path: Path) -> str | None:
    if not last_message_path.exists():
        return None
    try:
        payload = read_text(last_message_path).strip()
    except OSError:
        return None
    return payload or None


def extract_final_response_with_fallback(
    events: list[dict[str, Any]],
    *,
    last_message_path: Path,
) -> str | None:
    try:
        return extract_final_response(events)
    except ExecutorError:
        return load_final_response_from_path(last_message_path)


def resolve_change_request_payloads(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    payloads: Any,
) -> list[dict[str, Any]]:
    if payloads is None:
        return []
    if not isinstance(payloads, list):
        raise ExecutorError("Executor response change_requests must be an array.")
    resolved_payloads: list[dict[str, Any]] = []
    for index, raw in enumerate(payloads):
        if not isinstance(raw, dict):
            raise ExecutorError(f"Executor response change_requests[{index}] must be an object.")
        resolved = dict(raw)
        resolved.pop("conflicting_input_refs", None)
        resolved_payloads.append(resolved)
    return resolved_payloads


def resolve_blocker_payloads(payloads: Any) -> list[dict[str, Any]]:
    if payloads is None:
        return []
    if not isinstance(payloads, list):
        raise ExecutorError("Executor response blockers must be an array.")
    resolved_payloads: list[dict[str, Any]] = []
    for index, raw in enumerate(payloads):
        if not isinstance(raw, dict):
            raise ExecutorError(f"Executor response blockers[{index}] must be an object.")
        kind = str(raw.get("kind", "")).strip()
        summary = str(raw.get("summary", "")).strip()
        if not kind:
            raise ExecutorError(f"Executor response blockers[{index}] must include a kind.")
        if not summary:
            raise ExecutorError(f"Executor response blockers[{index}] must include a summary.")
        resolved_payloads.append(
            {
                "kind": kind,
                "summary": summary,
                "details": str(raw.get("details", "")).strip(),
                "related_paths": normalize_string_list(raw.get("related_paths")),
                "related_validation_ids": normalize_string_list(raw.get("related_validation_ids")),
                "suggested_owner_capability": str(raw.get("suggested_owner_capability", "")).strip() or None,
            }
        )
    return resolved_payloads


def normalize_executor_response_payload(parsed_response: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(parsed_response)
    if "produced_output_ids" not in normalized and isinstance(normalized.get("produced_outputs"), list):
        normalized["produced_output_ids"] = [
            output_id
            for output_id in (
                descriptor_output_id(item)
                for item in normalize_output_descriptors(
                    list(normalized.get("produced_outputs", [])),
                    allow_legacy_strings=False,
                )
            )
            if output_id
        ]
    normalized.setdefault("change_requests", [])
    if isinstance(normalized.get("change_requests"), list):
        cleaned_change_requests: list[Any] = []
        for raw in normalized["change_requests"]:
            if not isinstance(raw, dict):
                cleaned_change_requests.append(raw)
                continue
            cleaned = dict(raw)
            cleaned.pop("conflicting_input_refs", None)
            cleaned_change_requests.append(cleaned)
        normalized["change_requests"] = cleaned_change_requests
    normalized.setdefault("collaboration_request", None)
    normalized.setdefault("blockers", [])
    normalized.pop("produced_outputs", None)
    normalized.pop("context_echo", None)
    return normalized


def normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            continue
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def is_probable_local_contract_repair_request(task: dict[str, Any], payload: dict[str, Any]) -> bool:
    impacted_objective_ids = normalize_string_list(payload.get("impacted_objective_ids"))
    impacted_task_ids = normalize_string_list(payload.get("impacted_task_ids"))
    if impacted_objective_ids or impacted_task_ids:
        return False
    affected_handoff_ids = normalize_string_list(payload.get("affected_handoff_ids"))
    affected_output_ids = normalize_string_list(payload.get("affected_output_ids"))
    own_output_ids = {
        descriptor_output_id(item)
        for item in normalize_output_descriptors(list(task.get("expected_outputs", [])))
    }
    if affected_output_ids and not set(affected_output_ids).issubset(own_output_ids):
        return False
    change_category = str(payload.get("change_category", "")).strip().lower()
    if affected_handoff_ids and change_category not in {"ownership_boundary", "interface_contract"}:
        return False
    text_parts = [
        str(payload.get("summary", "")).strip().lower(),
        str(payload.get("blocking_reason", "")).strip().lower(),
        str(payload.get("why_local_resolution_is_invalid", "")).strip().lower(),
    ]
    haystack = " ".join(part for part in text_parts if part)
    contract_markers = (
        "allowed existing-file edits",
        "existing-file edits",
        "existing file",
        "already exists",
        "task contract",
        "write contract",
        "write scope",
        "write set",
        "ownership boundary",
        "entrypoint",
        "out of bounds",
        "forbids",
    )
    if any(marker in haystack for marker in contract_markers):
        return True
    if change_category == "ownership_boundary" and affected_output_ids and set(affected_output_ids).issubset(own_output_ids):
        return True
    return not affected_output_ids


def extract_local_contract_repair_requests(
    task: dict[str, Any],
    payloads: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not isinstance(payloads, list):
        return [], None
    remaining: list[dict[str, Any]] = []
    local_repairs: list[dict[str, Any]] = []
    for raw in payloads:
        if not isinstance(raw, dict):
            remaining.append(raw)
            continue
        if is_probable_local_contract_repair_request(task, raw):
            local_repairs.append(raw)
            continue
        remaining.append(raw)
    if not local_repairs:
        return remaining, None
    first = local_repairs[0]
    summary = str(first.get("summary", "")).strip() or "Repair the local task contract so the blocked task can continue."
    blocking_reason = str(first.get("blocking_reason", "")).strip()
    if blocking_reason:
        summary = f"{summary} Blocking reason: {blocking_reason}"
    return remaining, {
        "to_role": task["manager_role"],
        "type": "contract_resolution",
        "summary": summary,
        "blocking": True,
    }


def materialize_executor_response(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    parsed_response: dict[str, Any],
    *,
    runtime_warnings: list[dict[str, str]],
    runtime_recovery: dict[str, Any] | None,
    runtime_observability: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    run_dir = project_root / "runs" / run_id
    blocking_open_issues = [
        issue
        for issue in parsed_response.get("open_issues", [])
        if isinstance(issue, str) and issue_is_blocking(issue)
    ]
    if parsed_response["status"] == "ready_for_bundle_review" and blocking_open_issues:
        raise ExecutorError(
            "Executor response marked the task complete while reporting blocking issues: "
            + "; ".join(blocking_open_issues[:3])
        )
    blockers = resolve_blocker_payloads(parsed_response.get("blockers", []))
    if blockers and parsed_response["status"] != "blocked":
        raise ExecutorError("Executor response reported blockers without blocked status.")
    raw_change_requests = parsed_response.get("change_requests", [])
    raw_change_requests, synthesized_collaboration_payload = extract_local_contract_repair_requests(
        task,
        raw_change_requests,
    )
    resolved_change_requests = resolve_change_request_payloads(
        project_root,
        run_id,
        task,
        raw_change_requests,
    )
    normalized_change_requests = normalize_change_request_payloads(resolved_change_requests)
    if normalized_change_requests and parsed_response["status"] != "blocked":
        raise ExecutorError("Executor response reported change_requests without blocked status.")
    if parsed_response["status"] == "ready_for_bundle_review" and normalized_change_requests:
        raise ExecutorError("Executor response cannot be ready_for_bundle_review while requesting changes.")
    collaboration_ids: list[str] = []
    collaboration_payload = parsed_response.get("collaboration_request")
    if collaboration_payload is None:
        collaboration_payload = synthesized_collaboration_payload
    if isinstance(collaboration_payload, dict):
        request_id = next_collaboration_request_id(run_dir, task["task_id"])
        create_collaboration_request(
            project_root,
            run_id,
            request_id,
            task["objective_id"],
            task["assigned_role"],
            collaboration_payload["to_role"],
            collaboration_payload["type"],
            collaboration_payload["summary"],
            blocking=collaboration_payload["blocking"],
        )
        collaboration_ids.append(request_id)
    persisted_change_requests = persist_change_requests(project_root, run_id, task, normalized_change_requests)
    change_request_ids = [item["change_id"] for item in persisted_change_requests]

    report = {
        "schema": "completion-report.v1",
        "run_id": run_id,
        "phase": task["phase"],
        "objective_id": task["objective_id"],
        "task_id": task["task_id"],
        "agent_role": task["assigned_role"],
        "status": parsed_response["status"],
        "summary": parsed_response["summary"],
        "artifacts": parsed_response["artifacts"],
        "validation_results": parsed_response["validation_results"],
        "open_issues": parsed_response["open_issues"],
        "blockers": blockers,
        "produced_outputs": normalize_report_outputs(project_root, run_id, task, parsed_response),
        "change_requests": persisted_change_requests,
        "context_echo": system_context_echo(project_root, run_id, task),
    }
    if runtime_warnings:
        report["runtime_warnings"] = runtime_warnings
    if runtime_recovery is not None:
        report["runtime_recovery"] = runtime_recovery
    if runtime_observability is not None:
        report["runtime_observability"] = runtime_observability
    validate_document(report, "completion-report.v1", project_root)
    write_json(run_dir / "reports" / f"{task['task_id']}.json", report)
    return report, collaboration_ids, change_request_ids


def system_context_echo(project_root: Path, run_id: str, task: dict[str, Any]) -> dict[str, Any]:
    prompt_layers: list[str] = []
    prompt_log_path = project_root / "runs" / run_id / "prompt-logs" / f"{task['task_id']}.json"
    if prompt_log_path.exists():
        prompt_log = read_json(prompt_log_path)
        prompt_layers = [
            str(item).strip()
            for item in prompt_log.get("files_loaded", [])
            if isinstance(item, str) and str(item).strip()
        ]
    return {
        "role_id": task["assigned_role"],
        "objective_id": task["objective_id"],
        "phase": task["phase"],
        "prompt_layers": prompt_layers,
        "schema": task["schema"],
    }


def issue_is_blocking(issue: str) -> bool:
    normalized = issue.strip().lower()
    return normalized.startswith("blocking:") or normalized.startswith("blocked:")


def normalize_report_outputs(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    parsed_response: dict[str, Any],
) -> list[dict[str, Any]]:
    if parsed_response.get("status") != "ready_for_bundle_review":
        return []
    expected_by_id = output_descriptor_map(task.get("expected_outputs", []))
    raw_output_ids = parsed_response.get("produced_output_ids")
    if raw_output_ids is None and "produced_outputs" in parsed_response:
        raw_output_ids = [
            descriptor_output_id(item)
            for item in normalize_output_descriptors(list(parsed_response.get("produced_outputs", [])), allow_legacy_strings=False)
        ]
    produced_output_ids = [
        str(item).strip()
        for item in list(raw_output_ids or [])
        if str(item).strip()
    ]
    if parsed_response.get("status") == "ready_for_bundle_review" and not produced_output_ids:
        raise ExecutorError(f"Task {task['task_id']} completed without produced_output_ids")
    unique_output_ids: list[str] = []
    seen_output_ids: set[str] = set()
    for output_id in produced_output_ids:
        if output_id in seen_output_ids:
            continue
        seen_output_ids.add(output_id)
        unique_output_ids.append(output_id)
    unknown_output_ids = sorted(set(unique_output_ids) - set(expected_by_id))
    if unknown_output_ids:
        raise ExecutorError(
            f"Task {task['task_id']} reported outputs not declared in expected_outputs: {', '.join(unknown_output_ids)}"
        )
    passed_validation_ids = {
        str(item.get("id"))
        for item in parsed_response.get("validation_results", [])
        if isinstance(item, dict) and item.get("status") == "passed" and isinstance(item.get("id"), str)
    }
    search_roots = deliverable_search_roots(project_root, run_id, task["task_id"])
    canonical_outputs: list[dict[str, Any]] = []
    for output_id in unique_output_ids:
        expected = expected_by_id[output_id]
        expected_kind = str(expected.get("kind"))
        if expected_kind in {"artifact", "asset"}:
            path = str(expected.get("path", "")).strip()
            if not path:
                raise ExecutorError(f"Task {task['task_id']} reported output {output_id} without a path")
            if not repo_relative_path_exists(search_roots, path):
                raise ExecutorError(
                    f"Task {task['task_id']} reported output {output_id} at {path}, but the artifact does not exist"
                )
        elif expected_kind == "assertion":
            evidence = expected.get("evidence", {})
            validation_ids = [
                str(item).strip()
                for item in (evidence.get("validation_ids", []) if isinstance(evidence, dict) else [])
                if isinstance(item, str) and item.strip()
            ]
            missing_validation_ids = [validation_id for validation_id in validation_ids if validation_id not in passed_validation_ids]
            if missing_validation_ids:
                raise ExecutorError(
                    f"Task {task['task_id']} reported assertion output {output_id} without passed validations: "
                    + ", ".join(missing_validation_ids)
                )
            for artifact_path in [
                str(item).strip()
                for item in (evidence.get("artifact_paths", []) if isinstance(evidence, dict) else [])
                if isinstance(item, str) and item.strip()
            ]:
                if not repo_relative_path_exists(search_roots, artifact_path):
                    raise ExecutorError(
                        f"Task {task['task_id']} reported assertion output {output_id} with missing artifact evidence "
                        f"{artifact_path}"
                    )
        canonical_outputs.append(expected)
    return canonical_outputs


def deliverable_search_roots(project_root: Path, run_id: str, task_id: str) -> list[Path]:
    roots: list[Path] = [project_root]
    integration_workspace = project_root / ".orchestrator-worktrees" / run_id / "integration"
    if integration_workspace.exists():
        roots.append(integration_workspace)
    task_workspace = project_root / ".orchestrator-worktrees" / run_id / "tasks" / task_id
    if task_workspace.exists():
        roots.append(task_workspace)
    execution_path = project_root / "runs" / run_id / "executions" / f"{task_id}.json"
    if execution_path.exists():
        execution = read_json(execution_path)
        workspace_path = execution.get("workspace_path")
        if isinstance(workspace_path, str) and workspace_path.strip():
            workspace = Path(workspace_path)
            if not workspace.is_absolute():
                workspace = (project_root / workspace).resolve()
            if workspace.exists():
                roots.append(workspace)
    unique_roots: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique_roots.append(root)
    return unique_roots


def task_workspace_exists(project_root: Path, run_id: str, task_id: str) -> bool:
    return (project_root / ".orchestrator-worktrees" / run_id / "tasks" / task_id).exists()


def next_collaboration_request_id(run_dir: Path, task_id: str) -> str:
    index = 1
    while (run_dir / "collaboration" / f"{task_id}-CR-{index:03d}.json").exists():
        index += 1
    return f"{task_id}-CR-{index:03d}"


def accumulate_observability(
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
