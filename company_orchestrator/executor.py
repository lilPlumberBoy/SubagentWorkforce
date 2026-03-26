from __future__ import annotations

import json
import os
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
from .prompts import preview_resolved_inputs, render_prompt, resolve_report_artifact_path, resolve_workspace_input_path
from .recovery import prepare_activity_retry, reconcile_for_command
from .schemas import SchemaValidationError, validate_document
from .timeout_policy import resolve_task_timeout_policy, timeout_final_message, timeout_retry_message
from .worktree_manager import (
    WorkspaceInfo,
    WorktreeError,
    commit_task_workspace,
    ensure_task_workspace_with_refresh,
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


def coerce_process_text(stream: str | bytes | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


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

    task = read_json(task_path)
    normalize_task_artifact_descriptors(task)
    if task_requires_write_access(task) and task.get("execution_mode") == "read_only":
        task["execution_mode"] = "isolated_write"
    canonicalize_validation_commands(task)
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
    task_working_directory = runtime.workspace_path or task.get("working_directory")
    if task_working_directory:
        working_directory = Path(task_working_directory)
        if not working_directory.is_absolute():
            working_directory = (project_root / working_directory).resolve()
        else:
            working_directory = working_directory.resolve()
    else:
        working_directory = project_root
    materialize_task_context_files(project_root, run_id, task, working_directory)
    task_sandbox_mode = effective_sandbox_mode(task, sandbox_mode)
    prompt_metadata = render_prompt(
        project_root,
        run_id,
        task_path,
        working_directory=working_directory,
        sandbox_mode=task_sandbox_mode,
        task_payload=task,
    )
    prompt_text = read_text(project_root / prompt_metadata["prompt_path"])
    execution_prompt = build_execution_prompt(prompt_text)
    prompt_observability = prompt_metrics(execution_prompt)
    execution_dir = ensure_dir(run_dir / "executions")
    output_schema_path = project_root / "orchestrator" / "schemas" / "executor-response.v1.json"
    last_message_path = execution_dir / f"{task_id}.last-message.json"
    stdout_path = execution_dir / f"{task_id}.stdout.jsonl"
    stderr_path = execution_dir / f"{task_id}.stderr.log"
    summary_path = execution_dir / f"{task_id}.json"
    report_path = run_dir / "reports" / f"{task_id}.json"
    clear_text(stdout_path)
    clear_text(stderr_path)
    command = build_codex_command(
        codex_path=codex_path,
        working_directory=working_directory,
        output_schema_path=output_schema_path,
        last_message_path=last_message_path,
        sandbox_mode=task_sandbox_mode,
        additional_directories=task.get("additional_directories", []),
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

    def on_stdout_line(raw_line: str) -> None:
        append_text(stdout_path, raw_line + "\n")
        note_activity_stream(
            project_root,
            run_id,
            task_id,
            stdout_bytes=len((raw_line + "\n").encode("utf-8")),
        )
        handle_codex_event_line(project_root, run_id, task["phase"], task_id, raw_line)

    def on_stderr_line(raw_line: str) -> None:
        append_text(stderr_path, raw_line)
        note_activity_stream(
            project_root,
            run_id,
            task_id,
            stderr_bytes=len(raw_line.encode("utf-8")),
        )

    def on_process_started(process: subprocess.Popen[str]) -> None:
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

    stdout_attempts: list[str] = []
    stderr_attempts: list[str] = []
    total_attempts = timeout_policy.max_timeout_retries + 1
    completed: CodexProcessResult | None = None
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
                env=build_exec_environment(),
                timeout_seconds=timeout_policy.timeout_seconds,
                on_stdout_line=on_stdout_line,
                on_stderr_line=on_stderr_line,
                on_process_started=on_process_started,
            )
            call_completed_at = now_timestamp()
            call_latency_ms = int((time.monotonic() - call_started_monotonic) * 1000)
            break
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
    current_activity = read_activity(project_root, run_id, task_id)
    queue_wait_ms = int((current_activity.get("observability", {}) or {}).get("queue_wait_ms", 0))
    stdout_bytes = len(completed.stdout.encode("utf-8"))
    stderr_bytes = len(completed.stderr.encode("utf-8"))
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
        success=completed.returncode == 0 and extract_turn_failure(events) is None,
        input_tokens=int(usage.get("input_tokens", 0)),
        cached_input_tokens=int(usage.get("cached_input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        timeout_seconds=timeout_policy.timeout_seconds,
        error=extract_turn_failure(events),
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
    failure = extract_turn_failure(events)
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

    final_response = extract_final_response(events)
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
            "recovery_action": runtime.recovery_action or ("timeout_retry" if len(stdout_attempts) > 1 else None),
            "workspace_reused": runtime.workspace_reused,
            "timeout_retries_used": max(0, len(stdout_attempts) - 1),
        },
        runtime_observability=compact_observability_for_report(
            read_activity(project_root, run_id, task_id)["observability"]
        ),
    )
    if task.get("execution_mode", "read_only") == "isolated_write" and report["status"] == "ready_for_bundle_review":
        commit_result = commit_isolated_workspace(runtime, task_id)
        runtime.commit_sha = commit_result.get("commit_sha")
    recovery_action = runtime.recovery_action or ("timeout_retry" if len(stdout_attempts) > 1 else None)
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
    if not task_needs_workspace_snapshot(task):
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


def task_needs_workspace_snapshot(task: dict[str, Any]) -> bool:
    if task.get("execution_mode", "read_only") == "isolated_write":
        return True
    if referenced_task_output_ids(task):
        return True
    if any(isinstance(value, str) and value.strip() for value in task.get("handoff_dependencies", [])):
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


def materialize_task_context_files(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    working_directory: Path,
) -> None:
    if not working_directory.exists():
        return
    if working_directory.resolve() == project_root.resolve():
        return
    ensure_declared_output_parent_directories(task, working_directory)
    mirror_explicit_input_files_into_workspace(project_root, run_id, task, working_directory)
    mirror_resolved_input_files_into_workspace(project_root, run_id, task, working_directory)
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
            if destination.exists() or destination == source_path.resolve():
                continue
            shutil.copy2(source_path, destination)
    for source_task_id in sorted(source_task_ids):
        report_path = run_dir / "reports" / f"{source_task_id}.json"
        if not report_path.exists():
            continue
        mirror_report_into_workspace(project_root, run_id, source_task_id, report_path, working_directory)


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
) -> None:
    mirrored: set[str] = set()
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
        if destination_key in mirrored or destination.exists():
            mirrored.add(destination_key)
            continue
        ensure_dir(destination.parent)
        shutil.copy2(source_path, destination)
        mirrored.add(destination_key)


def mirror_resolved_input_files_into_workspace(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    working_directory: Path,
) -> None:
    resolved_inputs = preview_resolved_inputs(
        project_root,
        run_id,
        task,
        working_directory=working_directory,
        sandbox_mode=task.get("sandbox_mode"),
    )
    mirrored: set[str] = set()
    for candidate_path in collect_resolved_input_file_paths(resolved_inputs):
        source_path = resolve_workspace_input_path(project_root, run_id, candidate_path)
        if source_path is None or not source_path.is_file():
            continue
        destination = (working_directory / candidate_path).resolve()
        destination_key = str(destination)
        if destination_key in mirrored or destination.exists():
            mirrored.add(destination_key)
            continue
        ensure_dir(destination.parent)
        shutil.copy2(source_path, destination)
        mirrored.add(destination_key)


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
) -> None:
    report = read_json(report_path)
    report_destination = working_directory / "runs" / run_id / "reports" / f"{source_task_id}.json"
    ensure_dir(report_destination.parent)
    shutil.copy2(report_path, report_destination)
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
        shutil.copy2(source_path, destination)


def commit_isolated_workspace(runtime: TaskExecutionRuntime, task_id: str) -> dict[str, Any]:
    if not runtime.branch_name or not runtime.workspace_path:
        return {"committed": False, "commit_sha": None}
    try:
        return commit_task_workspace(
            WorkspaceInfo(branch_name=runtime.branch_name, workspace_path=Path(runtime.workspace_path)),
            task_id,
        )
    except WorktreeError as exc:
        raise ExecutorError(str(exc)) from exc


def build_exec_environment() -> dict[str, str]:
    env = dict(os.environ)
    # Force the CLI to rely on its existing ChatGPT login/session path.
    env.pop("CODEX_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
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
        + "\n\n# Executor Output Requirements\n\n"
        + "Use injected dependency artifact previews and resolved handoff payloads directly instead of rediscovering the same upstream files in sibling workspaces unless a validation command requires it.\n"
        + "Once the workspace root is known, use workspace-relative paths as the canonical form and do not probe both relative and absolute versions of the same path.\n"
        + "Do not waste turns on exploratory shell commands like `pwd`, `ls`, or repeated directory listings when the task assignment already declares the workspace root, owned paths, expected outputs, and resolved inputs.\n"
        + "Do not read unrelated package manifests, READMEs, or source/test trees unless they are explicitly referenced by the task assignment, owned paths, expected outputs, or resolved inputs.\n"
        + "Avoid no-op or duplicate shell commands once a path or artifact has already been confirmed.\n"
        + "Return only one JSON object matching the output schema.\n"
        + "Do not wrap the JSON in markdown fences.\n"
        + "Use the `# Exact Task Contract` section as the hard boundary for required outputs, allowed existing-file edits, and declared inputs.\n"
        + 'Use status "ready_for_bundle_review" when the task is complete.\n'
        + 'Use status "blocked" when another team, manager, or custodian must act before completion.\n'
        + 'If any open issue is still blocking completion, the status must be "blocked". Do not return "ready_for_bundle_review" with blocking open issues.\n'
        + "If injected design artifacts, runtime contracts, or handoff payloads contradict each other, stop at the source of the contradiction and report the exact conflicting paths or artifacts instead of guessing a merged contract.\n"
        + "If blocked because another team must answer or provide something, include a collaboration_request object.\n"
        + "If blocked because the goal now requires a cross-boundary contract, shared-behavior, ownership-boundary, or acceptance-rule change, include a change_requests array.\n"
        + "If blocked only because the task contract is locally inconsistent for an owned path (for example a required output file already exists but Allowed Existing-File Edits excludes it), do not emit change_requests. Emit a collaboration_request back to your manager for contract repair.\n"
        + "Do not use change_requests for local bug fixes, cleanup, naming, refactors, docs, or optional improvements.\n"
        + "A change request is valid only when it is goal-critical, blocking, impossible to resolve within your owned scope, and references the conflicting authoritative inputs or the affected shared outputs/handoffs.\n"
        + "When a cross-boundary blocker comes from injected upstream inputs, cite the narrowest exact entries from Input Source Metadata in conflicting_input_refs and leave affected_output_ids, affected_handoff_ids, impacted_objective_ids, and impacted_task_ids as empty arrays. The orchestrator resolves canonical impact ids.\n"
        + "Do not guess impact ids from your own local outputs when the blocker is caused by conflicting upstream contracts or handoffs.\n"
        + "Emit multiple change requests only when they are distinct root blockers with different conflicting inputs or affected shared outputs/handoffs.\n"
        + "Each change_requests entry must include these exact keys: change_category, summary, blocking_reason, why_local_resolution_is_invalid, blocking, goal_critical, affected_output_ids, affected_handoff_ids, impacted_objective_ids, impacted_task_ids, conflicting_input_refs, required_reentry_phase, impact.\n"
        + "Return produced_outputs as structured objects using the exact same output_id values declared in the task assignment expected_outputs.\n"
        + "Do not invent additional final produced_outputs beyond the outputs listed in the `# Exact Task Contract` section.\n"
        + "Each produced_outputs entry must include these exact keys: kind, output_id, path, asset_id, description, evidence.\n"
        + 'For artifact outputs: set kind="artifact", fill path, and set asset_id, description, evidence to null.\n'
        + 'For asset outputs: set kind="asset", fill asset_id and path, and set description and evidence to null.\n'
        + 'For assertion outputs: set kind="assertion", fill description and evidence={"validation_ids":[...],"artifact_paths":[...]}, and set path and asset_id to null.\n'
        + "Do not emit plain strings in produced_outputs.\n"
        + "Treat the injected Resolved Inputs as authoritative. If an `Output of <task-id>` input or handoff package is already present there, do not shell-search `runs/`, sibling task workspaces, or other task artifacts to rediscover the same context.\n"
        + "Only inspect the filesystem for a referenced upstream artifact when the Resolved Inputs clearly indicate that the artifact preview or payload is missing.\n"
        + "If the Task Assignment owns a new output path that is not present yet, create its parent directory and write the artifact directly instead of repeatedly probing sibling directories for the missing file.\n"
        + "For discovery/design artifact tasks, start by authoring the declared outputs from the injected inputs rather than using shell commands to rediscover the current working directory or confirm that the destination directories exist.\n"
        + "Do not re-read the generated prompt log or task prompt file from `runs/...` after launch unless the task assignment explicitly lists it as an input.\n"
        + "For discovery/design producing tasks, do not run `test -f`, `rg`, or `grep` against files you just created merely to prove they exist or contain required headings. Write the declared outputs and return.\n"
    )


def handle_codex_event_line(project_root: Path, run_id: str, phase: str, activity_id: str, raw_line: str) -> None:
    line = raw_line.strip()
    if not line:
        return
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
    event_type, message, payload, activity_updates = normalize_codex_event(event)
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=activity_id,
        event_type=event_type,
        message=message,
        payload=payload,
    )
    if activity_updates:
        update_activity(project_root, run_id, activity_id, **activity_updates)


def normalize_codex_event(event: dict[str, Any]) -> tuple[str, str, dict[str, Any], dict[str, Any] | None]:
    event_type = str(event.get("type", "codex.unknown"))
    if event_type == "thread.started":
        thread_id = event.get("thread_id")
        return (
            "codex.thread.started",
            f"Codex thread started: {thread_id}",
            {"thread_id": thread_id},
            {"status": "launching", "progress_stage": "launching", "current_activity": "Codex thread started."},
        )
    if event_type == "turn.started":
        return (
            "codex.turn.started",
            "Codex turn started.",
            {},
            {"status": "launching", "progress_stage": "launching", "current_activity": "Codex turn started."},
        )
    if event_type == "turn.completed":
        usage = event.get("usage", {})
        return (
            "codex.turn.completed",
            "Codex turn completed.",
            {"usage": usage if isinstance(usage, dict) else {}},
            {"status": "finalizing", "progress_stage": "finalizing", "current_activity": "Codex turn completed."},
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
        )
    if event_type == "error":
        message = str(event.get("message", "Codex stream error."))
        return (
            "codex.error",
            message,
            {"message": message},
            {"status": "failed", "progress_stage": "failed", "current_activity": message},
        )

    if event_type in {"item.started", "item.completed"}:
        item = event.get("item", {})
        if not isinstance(item, dict):
            return (f"codex.{event_type}", f"Codex {event_type}.", {}, None)
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
            )
        if item_type == "agent_message":
            text_preview = truncate_text(str(item.get("text", "")), 200)
            return (
                f"codex.{event_type}.agent_message",
                "Received final agent message.",
                {"item_id": item.get("id"), "text_preview": text_preview},
                {"status": "finalizing", "progress_stage": "finalizing", "current_activity": "Received final agent response."},
            )
        return (
            f"codex.{event_type}.{item_type}",
            f"Codex {event_type} for {item_type}.",
            {"item_id": item.get("id"), "item_type": item_type},
            None,
        )

    return (f"codex.{event_type}", f"Codex event {event_type}.", {}, None)


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
    for event in events:
        if event.get("type") == "turn.failed":
            error = event.get("error", {})
            if isinstance(error, dict):
                return str(error.get("message", "turn failed"))
            return "turn failed"
        if event.get("type") == "error":
            return str(event.get("message", "stream error"))
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
    input_source_metadata = build_task_input_source_metadata(project_root, run_id, task)
    resolved_payloads: list[dict[str, Any]] = []
    for index, raw in enumerate(payloads):
        if not isinstance(raw, dict):
            raise ExecutorError(f"Executor response change_requests[{index}] must be an object.")
        resolved = dict(raw)
        conflicting_input_refs = normalize_string_list(raw.get("conflicting_input_refs"))
        if not conflicting_input_refs:
            resolved_payloads.append(resolved)
            continue
        resolved_output_ids: list[str] = []
        resolved_handoff_ids: list[str] = []
        resolved_source_task_ids: list[str] = []
        unresolved_refs: list[str] = []
        for input_ref in conflicting_input_refs:
            metadata = input_source_metadata.get(input_ref)
            if not isinstance(metadata, dict) or not metadata.get("resolved"):
                unresolved_refs.append(input_ref)
                continue
            for output_id in normalize_string_list(metadata.get("output_ids")):
                if output_id not in resolved_output_ids:
                    resolved_output_ids.append(output_id)
            for handoff_id in normalize_string_list(metadata.get("handoff_ids")):
                if handoff_id not in resolved_handoff_ids:
                    resolved_handoff_ids.append(handoff_id)
            for source_task_id in normalize_string_list(metadata.get("source_task_ids")):
                if source_task_id not in resolved_source_task_ids:
                    resolved_source_task_ids.append(source_task_id)
        if unresolved_refs:
            refs = ", ".join(unresolved_refs)
            raise ExecutorError(
                f"Executor response change_requests[{index}] referenced unresolved conflicting_input_refs: {refs}."
            )
        if not resolved_output_ids and not resolved_handoff_ids:
            raise ExecutorError(
                f"Executor response change_requests[{index}] did not resolve any affected outputs or handoffs."
            )
        if resolved_source_task_ids and all(source_task_id == task["task_id"] for source_task_id in resolved_source_task_ids):
            raise ExecutorError(
                f"Executor response change_requests[{index}] cited only self-authored inputs in conflicting_input_refs."
            )
        resolved["conflicting_input_refs"] = conflicting_input_refs
        resolved["affected_output_ids"] = resolved_output_ids
        resolved["affected_handoff_ids"] = resolved_handoff_ids
        resolved["impacted_objective_ids"] = []
        resolved["impacted_task_ids"] = []
        resolved_payloads.append(resolved)
    return resolved_payloads


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
    conflicting_input_refs = normalize_string_list(payload.get("conflicting_input_refs"))
    affected_handoff_ids = normalize_string_list(payload.get("affected_handoff_ids"))
    if conflicting_input_refs or affected_handoff_ids:
        return False
    affected_output_ids = normalize_string_list(payload.get("affected_output_ids"))
    own_output_ids = {
        descriptor_output_id(item)
        for item in normalize_output_descriptors(list(task.get("expected_outputs", [])))
    }
    if affected_output_ids and not set(affected_output_ids).issubset(own_output_ids):
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
        "entrypoint",
        "out of bounds",
        "forbids",
    )
    if any(marker in haystack for marker in contract_markers):
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
        "produced_outputs": normalize_report_outputs(project_root, run_id, task, parsed_response),
        "change_requests": persisted_change_requests,
    }
    if runtime_warnings:
        report["runtime_warnings"] = runtime_warnings
    if runtime_recovery is not None:
        report["runtime_recovery"] = runtime_recovery
    if runtime_observability is not None:
        report["runtime_observability"] = runtime_observability
    if parsed_response.get("context_echo") is not None:
        report["context_echo"] = parsed_response["context_echo"]
    validate_document(report, "completion-report.v1", project_root)
    write_json(run_dir / "reports" / f"{task['task_id']}.json", report)
    return report, collaboration_ids, change_request_ids


def issue_is_blocking(issue: str) -> bool:
    normalized = issue.strip().lower()
    return normalized.startswith("blocking:") or normalized.startswith("blocked:")


def normalize_report_outputs(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    parsed_response: dict[str, Any],
) -> list[dict[str, Any]]:
    expected_by_id = output_descriptor_map(task.get("expected_outputs", []))
    produced_outputs = normalize_output_descriptors(list(parsed_response.get("produced_outputs", [])), allow_legacy_strings=False)
    if parsed_response.get("status") == "ready_for_bundle_review" and not produced_outputs:
        raise ExecutorError(f"Task {task['task_id']} completed without produced_outputs")
    produced_by_id = {descriptor_output_id(item): item for item in produced_outputs}
    unknown_output_ids = sorted(set(produced_by_id) - set(expected_by_id))
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
    for output_id, produced in produced_by_id.items():
        expected = expected_by_id[output_id]
        expected_kind = str(expected.get("kind"))
        produced_kind = str(produced.get("kind"))
        if produced_kind != expected_kind:
            raise ExecutorError(
                f"Task {task['task_id']} reported output {output_id} with kind {produced_kind}, expected {expected_kind}"
            )
        if expected_kind in {"artifact", "asset"}:
            path = str(produced.get("path", "")).strip()
            if not path:
                raise ExecutorError(f"Task {task['task_id']} reported output {output_id} without a path")
            expected_path = str(expected.get("path", "")).strip()
            if path != expected_path:
                raise ExecutorError(
                    f"Task {task['task_id']} reported output {output_id} at {path}, expected {expected_path}"
                )
            if expected_kind == "asset":
                expected_asset_id = str(expected.get("asset_id", "")).strip()
                produced_asset_id = str(produced.get("asset_id", "")).strip()
                if produced_asset_id != expected_asset_id:
                    raise ExecutorError(
                        f"Task {task['task_id']} reported asset output {output_id} with asset_id {produced_asset_id}, "
                        f"expected {expected_asset_id}"
                    )
            if not repo_relative_path_exists(search_roots, path):
                raise ExecutorError(
                    f"Task {task['task_id']} reported output {output_id} at {path}, but the artifact does not exist"
                )
        elif expected_kind == "assertion":
            if produced.get("path") is not None:
                raise ExecutorError(f"Task {task['task_id']} reported assertion output {output_id} with a non-null path")
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
