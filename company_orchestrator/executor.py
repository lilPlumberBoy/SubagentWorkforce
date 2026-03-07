from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .collaboration import create_collaboration_request
from .filesystem import ensure_dir, read_json, read_text, write_json, write_text
from .prompts import render_prompt
from .schemas import SchemaValidationError, validate_document


class ExecutorError(RuntimeError):
    pass


def coerce_process_text(stream: str | bytes | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


def execute_task(
    project_root: Path,
    run_id: str,
    task_id: str,
    *,
    sandbox_mode: str = "read-only",
    codex_path: str = "codex",
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    task_path = run_dir / "tasks" / f"{task_id}.json"
    if not task_path.exists():
        raise ExecutorError(f"Task {task_id} does not exist for run {run_id}")

    task = read_json(task_path)
    prompt_metadata = render_prompt(project_root, run_id, task_path)
    prompt_text = read_text(project_root / prompt_metadata["prompt_path"])
    execution_prompt = build_execution_prompt(prompt_text)
    execution_dir = ensure_dir(run_dir / "executions")
    output_schema_path = project_root / "orchestrator" / "schemas" / "executor-response.v1.json"
    last_message_path = execution_dir / f"{task_id}.last-message.json"
    stdout_path = execution_dir / f"{task_id}.stdout.jsonl"
    stderr_path = execution_dir / f"{task_id}.stderr.log"
    summary_path = execution_dir / f"{task_id}.json"
    working_directory = task.get("working_directory")
    command = build_codex_command(
        codex_path=codex_path,
        working_directory=Path(working_directory).resolve() if working_directory else project_root,
        output_schema_path=output_schema_path,
        last_message_path=last_message_path,
        sandbox_mode=task.get("sandbox_mode", sandbox_mode),
        additional_directories=task.get("additional_directories", []),
    )
    try:
        completed = subprocess.run(
            command,
            input=execution_prompt,
            text=True,
            capture_output=True,
            cwd=project_root,
            env=build_exec_environment(),
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = coerce_process_text(exc.stdout)
        stderr = coerce_process_text(exc.stderr)
        write_text(stdout_path, stdout)
        write_text(stderr_path, stderr)
        raise ExecutorError(f"codex exec timed out after {timeout_seconds} seconds for task {task_id}") from exc
    write_text(stdout_path, completed.stdout)
    write_text(stderr_path, completed.stderr)

    events = parse_jsonl_events(completed.stdout)
    failure = extract_turn_failure(events)
    if completed.returncode != 0 or failure is not None:
        message = failure or completed.stderr.strip() or f"codex exec exited with code {completed.returncode}"
        raise ExecutorError(message)

    final_response = extract_final_response(events)
    try:
        parsed_response = json.loads(final_response)
    except json.JSONDecodeError as exc:
        raise ExecutorError(f"Final response was not valid JSON: {final_response}") from exc

    try:
        validate_document(parsed_response, "executor-response.v1", project_root)
    except SchemaValidationError as exc:
        raise ExecutorError(f"Executor response failed schema validation: {exc}") from exc

    report, collaboration_ids = materialize_executor_response(project_root, run_id, task, parsed_response)
    execution_summary = {
        "task_id": task_id,
        "thread_id": extract_thread_id(events),
        "usage": extract_usage(events),
        "stdout_path": str(stdout_path.relative_to(project_root)),
        "stderr_path": str(stderr_path.relative_to(project_root)),
        "last_message_path": str(last_message_path.relative_to(project_root)),
        "report_path": str((run_dir / "reports" / f"{task_id}.json").relative_to(project_root)),
        "collaboration_request_ids": collaboration_ids,
        "status": report["status"],
    }
    write_json(summary_path, execution_summary)
    return execution_summary


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
    project_root: Path, run_id: str, task: dict[str, Any], parsed_response: dict[str, Any]
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
    if parsed_response.get("context_echo") is not None:
        report["context_echo"] = parsed_response["context_echo"]
    validate_document(report, "completion-report.v1", project_root)
    write_json(run_dir / "reports" / f"{task['task_id']}.json", report)
    return report, collaboration_ids


def next_collaboration_request_id(run_dir: Path, task_id: str) -> str:
    index = 1
    while (run_dir / "collaboration" / f"{task_id}-CR-{index:03d}.json").exists():
        index += 1
    return f"{task_id}-CR-{index:03d}"
