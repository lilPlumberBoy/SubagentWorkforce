from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .collaboration import create_collaboration_request
from .filesystem import ensure_dir, read_json, read_text, write_json, write_text
from .live import append_activity_warning, ensure_activity, now_timestamp, record_event, update_activity
from .prompts import render_prompt
from .recovery import prepare_activity_retry, reconcile_for_command
from .schemas import SchemaValidationError, validate_document
from .timeout_policy import resolve_task_timeout_policy, timeout_final_message, timeout_retry_message
from .worktree_manager import WorkspaceInfo, WorktreeError, commit_task_workspace, ensure_task_workspace


class ExecutorError(RuntimeError):
    pass


@dataclass
class CodexProcessResult:
    returncode: int
    stdout: str
    stderr: str


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
    on_process_started: Any | None = None,
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

    stdout_thread = threading.Thread(target=consume_stdout, daemon=True)
    stderr_thread = threading.Thread(target=consume_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        assert process.stdin is not None
        process.stdin.write(prompt)
        process.stdin.close()
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        raise subprocess.TimeoutExpired(
            cmd=command,
            timeout=timeout_seconds,
            output="".join(stdout_chunks),
            stderr="".join(stderr_chunks),
        ) from exc

    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    return CodexProcessResult(
        returncode=process.returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
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
    prompt_metadata = render_prompt(project_root, run_id, task_path)
    prompt_text = read_text(project_root / prompt_metadata["prompt_path"])
    execution_prompt = build_execution_prompt(prompt_text)
    execution_dir = ensure_dir(run_dir / "executions")
    output_schema_path = project_root / "orchestrator" / "schemas" / "executor-response.v1.json"
    last_message_path = execution_dir / f"{task_id}.last-message.json"
    stdout_path = execution_dir / f"{task_id}.stdout.jsonl"
    stderr_path = execution_dir / f"{task_id}.stderr.log"
    summary_path = execution_dir / f"{task_id}.json"
    report_path = run_dir / "reports" / f"{task_id}.json"
    working_directory = runtime.workspace_path or task.get("working_directory")
    command = build_codex_command(
        codex_path=codex_path,
        working_directory=Path(working_directory).resolve() if working_directory else project_root,
        output_schema_path=output_schema_path,
        last_message_path=last_message_path,
        sandbox_mode=task.get("sandbox_mode", sandbox_mode),
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
        handle_codex_event_line(project_root, run_id, task["phase"], task_id, raw_line)

    def on_process_started(process: subprocess.Popen[str]) -> None:
        update_activity(
            project_root,
            run_id,
            task_id,
            process_metadata={
                "pid": process.pid,
                "started_at": activity_state["updated_at"],
                "command": " ".join(command),
                "cwd": str(project_root),
            },
        )

    stdout_attempts: list[str] = []
    stderr_attempts: list[str] = []
    total_attempts = timeout_policy.max_timeout_retries + 1
    completed: CodexProcessResult | None = None
    for timeout_attempt in range(1, total_attempts + 1):
        try:
            completed = run_codex_command(
                command,
                prompt=execution_prompt,
                cwd=project_root,
                env=build_exec_environment(),
                timeout_seconds=timeout_policy.timeout_seconds,
                on_stdout_line=on_stdout_line,
                on_process_started=on_process_started,
            )
            break
        except subprocess.TimeoutExpired as exc:
            stdout_attempts.append(coerce_process_text(exc.stdout))
            stderr_attempts.append(coerce_process_text(exc.stderr))
            write_text(stdout_path, "".join(stdout_attempts))
            write_text(stderr_path, "".join(stderr_attempts))
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
    write_text(stdout_path, "".join(stdout_attempts))
    write_text(stderr_path, "".join(stderr_attempts))

    events = parse_jsonl_events(completed.stdout)
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

    report, collaboration_ids = materialize_executor_response(
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
        "usage": extract_usage(events),
        "stdout_path": str(stdout_path.relative_to(project_root)),
        "stderr_path": str(stderr_path.relative_to(project_root)),
        "last_message_path": str(last_message_path.relative_to(project_root)),
        "report_path": str(report_path.relative_to(project_root)),
        "collaboration_request_ids": collaboration_ids,
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
    }
    write_json(summary_path, execution_summary)
    return execution_summary


def prepare_task_runtime(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    *,
    runtime: TaskExecutionRuntime | None = None,
) -> TaskExecutionRuntime:
    resolved = runtime or TaskExecutionRuntime()
    if task.get("execution_mode", "read_only") != "isolated_write":
        return resolved
    existing_workspace = task_workspace_exists(project_root, run_id, task["task_id"])
    try:
        workspace = ensure_task_workspace(project_root, run_id, task["task_id"])
    except WorktreeError as exc:
        raise ExecutorError(str(exc)) from exc
    resolved.branch_name = workspace.branch_name
    resolved.workspace_path = str(workspace.workspace_path)
    resolved.workspace_reused = existing_workspace
    if existing_workspace:
        resolved.recovery_action = resolved.recovery_action or "reused_workspace"
    elif resolved.attempt > 1:
        resolved.recovery_action = resolved.recovery_action or "recreated_workspace"
    return resolved


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
    for directory in additional_directories:
        command.extend(["--add-dir", directory])
    return command


def build_execution_prompt(prompt_text: str) -> str:
    return (
        prompt_text
        + "\n\n# Executor Output Requirements\n\n"
        + "Return only one JSON object matching the output schema.\n"
        + "Do not wrap the JSON in markdown fences.\n"
        + 'Use status "ready_for_bundle_review" when the task is complete.\n'
        + 'Use status "blocked" when another team, manager, or custodian must act before completion.\n'
        + "If blocked by another team or shared asset, include a collaboration_request object.\n"
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


def materialize_executor_response(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
    parsed_response: dict[str, Any],
    *,
    runtime_warnings: list[dict[str, str]],
    runtime_recovery: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    run_dir = project_root / "runs" / run_id
    collaboration_ids: list[str] = []
    follow_up_requests = list(parsed_response["follow_up_requests"])
    collaboration_payload = parsed_response.get("collaboration_request")
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
        if request_id not in follow_up_requests:
            follow_up_requests.append(request_id)

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
        "dependency_impact": parsed_response["dependency_impact"],
        "open_issues": parsed_response["open_issues"],
        "follow_up_requests": follow_up_requests,
    }
    if runtime_warnings:
        report["runtime_warnings"] = runtime_warnings
    if runtime_recovery is not None:
        report["runtime_recovery"] = runtime_recovery
    if parsed_response.get("context_echo") is not None:
        report["context_echo"] = parsed_response["context_echo"]
    validate_document(report, "completion-report.v1", project_root)
    write_json(run_dir / "reports" / f"{task['task_id']}.json", report)
    return report, collaboration_ids


def task_workspace_exists(project_root: Path, run_id: str, task_id: str) -> bool:
    return (project_root / ".orchestrator-worktrees" / run_id / "tasks" / task_id).exists()


def next_collaboration_request_id(run_dir: Path, task_id: str) -> str:
    index = 1
    while (run_dir / "collaboration" / f"{task_id}-CR-{index:03d}.json").exists():
        index += 1
    return f"{task_id}-CR-{index:03d}"
