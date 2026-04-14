from __future__ import annotations

from datetime import datetime, timezone
import time
import threading
import hashlib
from pathlib import Path
from typing import Any, Callable, TypeVar

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from .autonomy import autonomy_history_path, read_autonomy_state
from .filesystem import load_optional_json, read_text
from .handoffs import list_handoffs
from .live import list_activities, read_activity, read_activity_history, read_events, read_run_state, refresh_run_state
from .management import run_guidance
from .observability import read_run_observability

T = TypeVar("T")


def watch_run(
    project_root: Path,
    run_id: str,
    *,
    refresh_seconds: float = 1.0,
) -> None:
    console = Console()
    try:
        with Live(
            build_run_dashboard(project_root, run_id),
            console=console,
            refresh_per_second=4,
        ) as live:
            while True:
                time.sleep(refresh_seconds)
                live.update(
                    build_run_dashboard(project_root, run_id),
                    refresh=True,
                )
    except KeyboardInterrupt:
        return


def inspect_activity(
    project_root: Path,
    run_id: str,
    activity_id: str,
    *,
    follow: bool = False,
    events: int = 20,
) -> None:
    console = Console()
    try:
        initial_render = build_activity_detail(project_root, run_id, activity_id, events=events)
    except FileNotFoundError as exc:
        raise SystemExit(f"Activity {activity_id} was not found in run {run_id}.") from exc
    if not follow:
        console.print(initial_render)
        return
    try:
        with Live(initial_render, console=console, refresh_per_second=4) as live:
            while True:
                time.sleep(1.0)
                live.update(build_activity_detail(project_root, run_id, activity_id, events=events), refresh=True)
    except KeyboardInterrupt:
        return


def debug_prompt(
    project_root: Path,
    run_id: str,
    activity_id: str,
    *,
    follow: bool = False,
    events: int = 20,
    show_body: bool = True,
) -> None:
    console = Console()
    try:
        initial_render = build_prompt_debug_detail(
            project_root,
            run_id,
            activity_id,
            events=events,
            show_body=show_body,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"Activity {activity_id} was not found in run {run_id}.") from exc
    if not follow:
        console.print(initial_render)
        return
    try:
        with Live(initial_render, console=console, refresh_per_second=4) as live:
            while True:
                time.sleep(1.0)
                live.update(
                    build_prompt_debug_detail(
                        project_root,
                        run_id,
                        activity_id,
                        events=events,
                        show_body=show_body,
                    ),
                    refresh=True,
                )
    except KeyboardInterrupt:
        return


def run_with_watch(
    project_root: Path,
    run_id: str,
    operation: Callable[[], T],
    *,
    refresh_seconds: float = 1.0,
) -> T:
    console = Console()
    result: dict[str, T] = {}
    error: dict[str, BaseException] = {}
    completed = threading.Event()

    def runner() -> None:
        try:
            result["value"] = operation()
        except BaseException as exc:
            error["value"] = exc
        finally:
            completed.set()

    thread = threading.Thread(target=runner, name=f"watch-run:{run_id}")
    thread.start()
    with Live(
        build_run_dashboard(project_root, run_id),
        console=console,
        refresh_per_second=4,
    ) as live:
        while not completed.wait(refresh_seconds):
            live.update(
                build_run_dashboard(project_root, run_id),
                refresh=True,
            )
        live.update(
            build_run_dashboard(project_root, run_id),
            refresh=True,
        )
    thread.join()
    if "value" in error:
        raise error["value"]
    return result["value"]


def build_run_dashboard(project_root: Path, run_id: str):
    refresh_run_state(project_root, run_id)
    run_state = read_run_state(project_root, run_id)
    current_phase = run_state["current_phase"]
    activities = list_activities(project_root, run_id, phase=current_phase)
    history = read_activity_history(project_root, run_id)
    handoffs = list_handoffs(project_root / "runs" / run_id, phase=current_phase)
    active_plans = [
        activity
        for activity in activities
        if activity["kind"] in {"objective_plan", "capability_plan"}
        and activity["status"] not in {"completed", "failed", "interrupted", "recovered", "abandoned"}
    ]
    active_tasks = [activity for activity in activities if activity["kind"] == "task_execution" and is_active(activity)]
    queued_tasks = [activity for activity in activities if activity["kind"] == "task_execution" and activity["status"] == "queued"]
    blocked_tasks = [
        activity
        for activity in activities
        if activity["kind"] == "task_execution" and activity["status"] in {"waiting_dependencies", "blocked"}
    ]
    interrupted_activities = [
        activity
        for activity in activities
        if activity["status"] in {"interrupted", "recovered", "abandoned"}
    ]
    objective_map = load_optional_json(project_root / "runs" / run_id / "objective-map.json") or {"objectives": []}
    objectives = objective_map.get("objectives", [])
    objective_lookup = build_objective_lookup(objectives)

    return Group(
        build_run_header(run_id, run_state),
        build_autonomy_panel(project_root, run_id),
        build_counts_table(run_state),
        build_observability_panel(project_root, run_id),
        build_objective_progress_table(objectives, activities, objective_lookup),
        build_activity_table("Active Planning Activities", active_plans, objective_lookup),
        build_activity_table("Active Task Activities", active_tasks, objective_lookup),
        build_activity_table("Queued Tasks", queued_tasks, objective_lookup),
        build_activity_table("Blocked Tasks", blocked_tasks, objective_lookup),
        build_handoff_table("Collaboration Handoffs", handoffs, objective_lookup),
        build_activity_table("Interrupted / Recovered Activities", interrupted_activities, objective_lookup),
        build_warning_rollup_panel(activities, objective_lookup),
        build_recovery_rollup_panel(activities),
        build_phase_progress_panel(objectives, activities),
        build_activity_history_panel(history, objective_lookup),
        build_run_guidance_panel(project_root, run_id, current_phase),
    )


def build_activity_detail(project_root: Path, run_id: str, activity_id: str, *, events: int = 20):
    activity = read_activity(project_root, run_id, activity_id)
    prompt_text = load_prompt_text(project_root, activity.get("prompt_path"))
    event_rows = read_events(project_root, run_id, activity_id=activity_id)[-events:]
    return Group(
        build_activity_summary_panel(activity),
        Panel(prompt_text, title="Prompt", border_style="cyan"),
        build_event_table(event_rows, title=f"Latest Events ({len(event_rows)})"),
        build_artifact_paths_panel(activity),
    )


def build_prompt_debug_detail(
    project_root: Path,
    run_id: str,
    activity_id: str,
    *,
    events: int = 20,
    show_body: bool = True,
):
    activity = read_activity(project_root, run_id, activity_id)
    event_rows = read_events(project_root, run_id, activity_id=activity_id)[-events:]
    renderables = [
        build_prompt_debug_panel(activity),
        build_event_table(event_rows, title=f"Latest Events ({len(event_rows)})"),
    ]
    if show_body:
        renderables.insert(1, Panel(load_prompt_text(project_root, activity.get("prompt_path")), title="Prompt Body", border_style="cyan"))
    return Group(*renderables)


def build_run_header(run_id: str, run_state: dict[str, Any]) -> Panel:
    text = Text()
    text.append(f"Run: {run_id}\n", style="bold")
    text.append(f"Phase: {run_state['current_phase']}\n")
    text.append(f"Updated: {run_state['updated_at']} ({age_text(run_state['updated_at'])})")
    return Panel(text, title="Run Status", border_style="green")


def build_autonomy_panel(project_root: Path, run_id: str) -> Panel:
    state = read_autonomy_state(project_root, run_id)
    run_state = read_run_state(project_root, run_id)
    guidance = run_guidance(project_root, run_id, phase=run_state["current_phase"])
    border_style = {
        "inactive": "blue",
        "running": "green",
        "waiting_for_approval": "yellow",
        "stopped": "yellow",
        "completed": "cyan",
    }.get(state["status"], "blue")
    lines = [
        f"Controller status: {state['status']}",
        f"Run status: {guidance['run_status']}",
        f"Auto approve: {state['auto_approve']}",
        f"Approval scope: {state.get('approval_scope', 'all')}",
        f"Stop before phases: {', '.join(state.get('stop_before_phases', [])) or 'none'}",
        f"Stop on recovery: {state.get('stop_on_recovery', False)}",
        f"Adaptive tuning: {state.get('adaptive_tuning', True)}",
        f"Sandbox: {state['sandbox_mode']}",
        f"Max concurrency: {state['max_concurrency']}",
        f"Timeout: {state['timeout_seconds'] if state['timeout_seconds'] is not None else 'policy default'}",
        f"Active phase: {state.get('active_phase') or 'none'}",
        f"Last action: {state.get('last_action') or 'none'}",
        f"Last action status: {state.get('last_action_status') or 'none'}",
        f"Stop reason: {state.get('stop_reason') or 'none'}",
    ]
    if state.get("approval_scope") == "none":
        lines.append("Review gates: disabled for autonomous advancement.")
    if state["status"] == "waiting_for_approval":
        lines.append("Execution note: the controller is paused at a human review gate.")
    elif state["status"] != "running" and guidance["run_status"] == "working":
        lines.append("Execution note: run work is active outside the autonomous controller.")
    elif state["status"] != "running" and guidance["run_status"] == "recoverable":
        lines.append("Execution note: the run can continue, but the autonomous controller is not currently attached.")
    tuning = state.get("last_tuning_decision")
    if tuning:
        lines.append(
            "Last tuning: "
            f"{tuning.get('action_kind') or 'n/a'} "
            f"{tuning.get('requested_max_concurrency')}→{tuning.get('effective_max_concurrency')} "
            f"({tuning.get('reason')})"
        )
    audit_path = autonomy_history_path(project_root, run_id)
    lines.append(f"Audit log: {audit_path.relative_to(project_root) if audit_path.exists() else 'not written yet'}")
    return Panel("\n".join(lines), title="Autonomy Controller", border_style=border_style)


def build_counts_table(run_state: dict[str, Any]) -> Table:
    table = Table(title="Activity Counts", expand=True)
    table.add_column("Type")
    table.add_column("Count")
    for status, count in sorted(run_state.get("counts_by_status", {}).items()):
        table.add_row(f"status:{status}", str(count))
    for kind, count in sorted(run_state.get("counts_by_kind", {}).items()):
        table.add_row(f"kind:{kind}", str(count))
    if table.row_count == 0:
        table.add_row("none", "0")
    return table


def build_phase_progress_panel(objectives: list[dict[str, Any]], activities: list[dict[str, Any]]) -> Panel:
    fractions = [objective_progress_fraction(objective["objective_id"], activities) for objective in objectives]
    phase_fraction = sum(fractions) / len(fractions) if fractions else 0.0
    return Panel(progress_renderable(phase_fraction), title=f"Phase Progress ({percent_text(phase_fraction)})", border_style="magenta")


def build_objective_progress_table(
    objectives: list[dict[str, Any]],
    activities: list[dict[str, Any]],
    objective_lookup: dict[str, dict[str, str]],
) -> Table:
    table = Table(title="Objective Progress", expand=True)
    table.add_column("Objective")
    table.add_column("Progress")
    table.add_column("Status Summary")
    if not objectives:
        table.add_row("none", progress_renderable(0.0), "no objectives")
        return table
    for objective in objectives:
        objective_id = objective["objective_id"]
        fraction = objective_progress_fraction(objective_id, activities)
        statuses = summarize_objective_statuses(objective_id, activities)
        table.add_row(objective_label(objective_id, objective_lookup), progress_renderable(fraction), statuses)
    return table


def build_activity_table(title: str, activities: list[dict[str, Any]], objective_lookup: dict[str, dict[str, str]]) -> Table:
    table = Table(title=title, expand=True)
    table.add_column("Activity")
    table.add_column("Objective")
    table.add_column("Status")
    table.add_column("LLM")
    table.add_column("Warnings")
    table.add_column("Progress")
    table.add_column("Elapsed")
    table.add_column("Last Event")
    table.add_column("Current")
    if not activities:
        table.add_row("none", "-", "-", "-", "-", progress_renderable(0.0), "-", "-", "-")
        return table
    for activity in sorted(activities, key=activity_sort_key):
        warnings_text = "; ".join(item["message"] for item in activity.get("warnings", [])) or "-"
        table.add_row(
            activity_label(activity),
            objective_label(activity["objective_id"], objective_lookup),
            status_label(activity),
            activity_observability_summary(activity),
            warnings_text,
            progress_renderable(activity["progress_fraction"]),
            elapsed_text(activity),
            last_event_age_text(activity),
            activity.get("current_activity") or "-",
        )
    return table


def build_activity_summary_panel(activity: dict[str, Any]) -> Panel:
    observability = activity.get("observability", {})
    lines = [
        f"Display ID: {activity_code(activity)}",
        f"Activity: {activity['activity_id']}",
        f"Kind: {activity['kind']}",
        f"Objective: {activity['objective_id']}",
        f"Phase: {activity['phase']}",
        f"Role: {activity.get('assigned_role') or '-'}",
        f"Status: {activity['status']}",
        f"Attempt: {activity.get('attempt', 1)}",
        f"Stage: {activity['progress_stage']}",
        f"Progress: {percent_text(activity['progress_fraction'])}",
        f"Elapsed: {elapsed_text(activity)}",
        f"Latest event: {activity.get('latest_event', {}).get('event_type') or '-'}",
        f"Last event age: {last_event_age_text(activity)}",
        f"Status reason: {activity.get('status_reason') or '-'}",
        f"Recovery action: {activity.get('recovery_action') or '-'}",
        f"Interrupted at: {activity.get('interrupted_at') or '-'}",
        f"Recovered at: {activity.get('recovered_at') or '-'}",
        f"Parallel requested: {activity.get('parallel_execution_requested', False)}",
        f"Parallel granted: {activity.get('parallel_execution_granted', False)}",
        f"Fallback reason: {activity.get('parallel_fallback_reason') or '-'}",
        f"Workspace: {activity.get('workspace_path') or '-'}",
        f"Branch: {activity.get('branch_name') or '-'}",
        f"Prompt size: {observability.get('prompt_char_count', 0)} chars / {observability.get('prompt_line_count', 0)} lines",
        f"Prompt bytes: {observability.get('prompt_bytes', 0)}",
        f"Submitted: {observability.get('submitted_at') or '-'}",
        f"Launch started: {observability.get('launched_at') or '-'}",
        f"Thread started: {observability.get('thread_started_at') or '-'}",
        f"Turn started: {observability.get('turn_started_at') or '-'}",
        f"First stream: {observability.get('first_stream_at') or '-'}",
        f"Turn completed: {observability.get('turn_completed_at') or '-'}",
        f"LLM calls: {observability.get('llm_call_count', 0)}",
        f"Tokens: in={observability.get('input_tokens', 0)} cached={observability.get('cached_input_tokens', 0)} out={observability.get('output_tokens', 0)}",
        (
            "Latency: "
            f"last={humanize_ms(int(observability.get('last_call_latency_ms', 0)))} "
            f"queue={humanize_ms(int(observability.get('queue_wait_ms', 0)))} "
            f"first_stream={humanize_ms(int(observability.get('time_to_first_stream_ms', 0)))} "
            f"processing={humanize_ms(int(observability.get('processing_ms', 0)))} "
            f"runtime={humanize_ms(int(observability.get('runtime_ms', 0)))} "
            f"wall={humanize_ms(int(observability.get('wall_clock_ms', 0)))}"
        ),
        f"Timeouts / retries: {observability.get('timeout_count', 0)} / {observability.get('timeout_retry_count', 0)}",
        f"LLM stream bytes: stdout={observability.get('stdout_bytes', 0)} stderr={observability.get('stderr_bytes', 0)}",
        f"In-flight signal: last={relative_timestamp(observability.get('last_signal_at'))} stdout={relative_timestamp(observability.get('last_stdout_at'))} stderr={relative_timestamp(observability.get('last_stderr_at'))}",
        f"In-flight stream bytes: stdout={observability.get('stream_stdout_bytes', 0)} stderr={observability.get('stream_stderr_bytes', 0)}",
        f"Current: {activity.get('current_activity') or '-'}",
        f"Updated: {activity['updated_at']}",
    ]
    warnings = activity.get("warnings", [])
    if warnings:
        lines.append("Warnings:")
        for item in warnings:
            lines.append(f"- {item['code']}: {item['message']}")
    return Panel("\n".join(lines), title="Activity", border_style="yellow")


def build_prompt_debug_panel(activity: dict[str, Any]) -> Panel:
    observability = activity.get("observability", {})
    lines = [
        f"Activity: {activity['activity_id']}",
        f"Kind: {activity['kind']}",
        f"Status: {activity['status']}",
        f"Attempt: {activity.get('attempt', 1)}",
        f"Prompt path: {activity.get('prompt_path') or '-'}",
        f"Prompt size: {observability.get('prompt_char_count', 0)} chars / {observability.get('prompt_line_count', 0)} lines / {observability.get('prompt_bytes', 0)} bytes",
        f"Submitted: {observability.get('submitted_at') or '-'}",
        f"Launch started: {observability.get('launched_at') or '-'}",
        f"Thread started: {observability.get('thread_started_at') or '-'}",
        f"Turn started: {observability.get('turn_started_at') or '-'}",
        f"First stream: {observability.get('first_stream_at') or '-'}",
        f"Turn completed: {observability.get('turn_completed_at') or '-'}",
        f"Queue wait: {humanize_ms(int(observability.get('queue_wait_ms', 0)))}",
        f"Time to first stream: {humanize_ms(int(observability.get('time_to_first_stream_ms', 0)))}",
        f"Processing: {humanize_ms(int(observability.get('processing_ms', 0)))}",
        f"Runtime: {humanize_ms(int(observability.get('runtime_ms', 0)))}",
        f"Wall clock: {humanize_ms(int(observability.get('wall_clock_ms', 0)))}",
        f"Tokens: in={observability.get('input_tokens', 0)} cached={observability.get('cached_input_tokens', 0)} out={observability.get('output_tokens', 0)}",
        f"Stdout/Stderr bytes: {observability.get('stdout_bytes', 0)} / {observability.get('stderr_bytes', 0)}",
        f"Workspace: {activity.get('workspace_path') or '-'}",
        f"Branch: {activity.get('branch_name') or '-'}",
        f"Stdout path: {activity.get('stdout_path') or '-'}",
        f"Stderr path: {activity.get('stderr_path') or '-'}",
        f"Output path: {activity.get('output_path') or '-'}",
    ]
    return Panel("\n".join(lines), title="Prompt Debug", border_style="magenta")


def build_observability_panel(project_root: Path, run_id: str) -> Panel:
    summary = read_run_observability(project_root, run_id)
    lines = [
        f"Calls: {summary['total_calls']} total, {summary['completed_calls']} completed, {summary['failed_calls']} failed, {summary['timed_out_calls']} timed out",
        f"Tokens: in={summary['total_input_tokens']} cached={summary['total_cached_input_tokens']} out={summary['total_output_tokens']}",
        f"Prompt volume: {summary['total_prompt_chars']} chars across {summary['total_prompt_lines']} lines",
        f"Latency: avg={humanize_ms(int(summary['average_latency_ms']))} max={humanize_ms(int(summary['max_latency_ms']))}",
        f"Queue wait: avg={humanize_ms(int(summary['average_queue_wait_ms']))}",
        f"Retries scheduled: {summary['retry_scheduled_calls']}",
        f"Active processes: {summary['active_processes']}",
        f"Active stream bytes: stdout={summary['active_stream_stdout_bytes']} stderr={summary['active_stream_stderr_bytes']}",
        f"Oldest active runtime / signal age: {humanize_ms(int(summary['max_active_runtime_ms']))} / {humanize_ms(int(summary['max_last_signal_age_ms']))}",
        f"Active calls by kind: {format_counts(summary['active_calls_by_kind'])}",
        f"Calls by kind: {format_counts(summary['calls_by_kind'])}",
    ]
    return Panel("\n".join(lines), title="LLM Observability", border_style="blue")


def build_handoff_table(title: str, handoffs: list[dict[str, Any]], objective_lookup: dict[str, dict[str, str]]) -> Table:
    table = Table(title=title, expand=True)
    table.add_column("Handoff")
    table.add_column("Objective")
    table.add_column("Status")
    table.add_column("From")
    table.add_column("To Tasks")
    table.add_column("Blocking")
    if not handoffs:
        table.add_row("none", "-", "-", "-", "-", "-")
        return table
    for handoff in sorted(handoffs, key=lambda item: (item["objective_id"], item["handoff_id"])):
        table.add_row(
            stable_code("HOF", handoff["handoff_id"]) + f" · {handoff['handoff_id']}",
            objective_label(handoff["objective_id"], objective_lookup),
            f"{handoff['status']} ({handoff.get('status_reason') or '-'})",
            handoff["from_task_id"],
            ", ".join(handoff.get("to_task_ids", [])) or "-",
            "yes" if handoff.get("blocking") else "no",
        )
    return table


def build_event_table(events: list[dict[str, Any]], *, title: str) -> Table:
    table = Table(title=title, expand=True)
    table.add_column("Timestamp")
    table.add_column("Type")
    table.add_column("Message")
    if not events:
        table.add_row("-", "-", "No events recorded.")
        return table
    for event in events:
        table.add_row(event["timestamp"], event["event_type"], event["message"])
    return table


def build_artifact_paths_panel(activity: dict[str, Any]) -> Panel:
    lines = [
        f"Prompt: {activity.get('prompt_path') or '-'}",
        f"Stdout: {activity.get('stdout_path') or '-'}",
        f"Stderr: {activity.get('stderr_path') or '-'}",
        f"Output: {activity.get('output_path') or '-'}",
        f"Workspace: {activity.get('workspace_path') or '-'}",
        f"Branch: {activity.get('branch_name') or '-'}",
    ]
    return Panel("\n".join(lines), title="Artifacts", border_style="blue")


def activity_observability_summary(activity: dict[str, Any]) -> str:
    observability = activity.get("observability", {}) or {}
    return (
        f"wait {humanize_ms(int(observability.get('queue_wait_ms', 0)))} · "
        f"run {humanize_ms(int(observability.get('runtime_ms', 0)))} · "
        f"in {int(observability.get('input_tokens', 0))} · "
        f"out {int(observability.get('output_tokens', 0))}"
    )


def build_warning_rollup_panel(activities: list[dict[str, Any]], objective_lookup: dict[str, dict[str, str]]) -> Panel:
    warning_lines = []
    for activity in sorted(activities, key=activity_sort_key):
        for item in activity.get("warnings", []):
            warning_lines.append(
                f"- {activity_label(activity)} ({objective_label(activity['objective_id'], objective_lookup)}): {item['message']}"
            )
    if not warning_lines:
        warning_lines = ["- none"]
    return Panel("\n".join(warning_lines), title="Parallelism Warnings", border_style="red")


def build_recovery_rollup_panel(activities: list[dict[str, Any]]) -> Panel:
    lines = []
    for activity in sorted(activities, key=activity_sort_key):
        if activity["status"] not in {"interrupted", "recovered", "abandoned"}:
            continue
        lines.append(
            f"- {activity_label(activity)}: "
            f"{activity['status']} ({activity.get('status_reason') or activity.get('recovery_action') or '-'})"
        )
    if not lines:
        lines = ["- none"]
    return Panel("\n".join(lines), title="Recovery Actions", border_style="yellow")


def build_run_guidance_panel(project_root: Path, run_id: str, phase: str) -> Panel:
    run_dir = project_root / "runs" / run_id
    tasks = []
    for path in sorted((run_dir / "tasks").glob("*.json")):
        payload = load_optional_json(path)
        if payload and payload.get("phase") == phase:
            tasks.append(payload)
    manager_summary = load_optional_json(run_dir / "manager-runs" / f"phase-{phase}.json") or {}
    guidance = run_guidance(
        project_root,
        run_id,
        phase=phase,
        tasks=tasks,
        scheduler_summary=manager_summary.get("scheduled", {}),
    )
    status = guidance["run_status"]
    border_style = {
        "working": "green",
        "ready_for_review": "yellow",
        "ready_to_advance": "cyan",
        "recoverable": "magenta",
        "blocked": "red",
    }.get(status, "green")
    lines = [
        f"Status: {status}",
        f"Reason: {guidance['run_status_reason']}",
        f"Next action: {guidance.get('next_action_command') or 'none'}",
        f"Action reason: {guidance['next_action_reason']}",
        f"Review doc: {guidance.get('review_doc_path') or 'none'}",
        f"Recommendation: {guidance.get('phase_recommendation') or 'none'}",
    ]
    return Panel("\n".join(lines), title="Next Action", border_style=border_style)


def build_activity_history_panel(history: list[dict[str, Any]], objective_lookup: dict[str, dict[str, str]]) -> Panel:
    terminal = sorted(history, key=lambda item: item["timestamp"], reverse=True)[:12]
    lines = []
    for activity in terminal:
        lines.append(
            f"- {history_activity_label(activity)} "
            f"({objective_label(activity['objective_id'], objective_lookup)}): "
            f"{activity['status']} at {activity['timestamp']}"
        )
    if not lines:
        lines = ["- none"]
    return Panel("\n".join(lines), title="Activity History", border_style="green")


def load_prompt_text(project_root: Path, prompt_path: str | None) -> str:
    if not prompt_path:
        return "Prompt is not available for this activity yet."
    path = project_root / prompt_path
    if not path.exists():
        return f"Prompt file not found: {prompt_path}"
    return read_text(path)


def objective_progress_fraction(objective_id: str, activities: list[dict[str, Any]]) -> float:
    objective_activities = [activity for activity in activities if activity["objective_id"] == objective_id]
    if not objective_activities:
        return 0.0
    return sum(activity["progress_fraction"] for activity in objective_activities) / len(objective_activities)


def summarize_objective_statuses(objective_id: str, activities: list[dict[str, Any]]) -> str:
    objective_activities = [activity for activity in activities if activity["objective_id"] == objective_id]
    if not objective_activities:
        return "no activity"
    counts: dict[str, int] = {}
    for activity in objective_activities:
        counts[activity["status"]] = counts.get(activity["status"], 0) + 1
    return ", ".join(f"{status}:{count}" for status, count in sorted(counts.items()))


def progress_renderable(fraction: float):
    clamped = max(0.0, min(1.0, fraction))
    return Group(
        ProgressBar(total=100, completed=int(clamped * 100), width=20),
        Text(percent_text(clamped), style="bold"),
    )


def percent_text(fraction: float) -> str:
    return f"{int(max(0.0, min(1.0, fraction)) * 100)}%"


def humanize_ms(value: int) -> str:
    if value <= 0:
        return "0ms"
    if value < 1000:
        return f"{value}ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    return f"{minutes}m {remaining}s"


def format_counts(payload: dict[str, Any]) -> str:
    if not payload:
        return "none"
    return ", ".join(f"{key}:{value}" for key, value in sorted(payload.items()))


def format_prompt_tokens(payload: dict[str, Any]) -> str:
    return (
        f"in={int(payload.get('input_tokens', 0))} "
        f"cached={int(payload.get('cached_input_tokens', 0))} "
        f"out={int(payload.get('output_tokens', 0))}"
    )


def is_active(activity: dict[str, Any]) -> bool:
    return activity["status"] not in {
        "queued",
        "waiting_dependencies",
        "ready_for_bundle_review",
        "blocked",
        "needs_revision",
        "completed",
        "failed",
        "accepted",
        "rejected",
        "skipped_existing",
        "interrupted",
        "recovered",
        "abandoned",
    }


def activity_sort_key(activity: dict[str, Any]) -> tuple[int, str]:
    queue_position = activity.get("queue_position")
    if queue_position is None:
        return (999999, activity["activity_id"])
    return (int(queue_position), activity["activity_id"])


def build_objective_lookup(objectives: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for objective in objectives:
        objective_id = objective["objective_id"]
        lookup[objective_id] = {
            "code": stable_code("OBJ", objective_id),
            "title": objective.get("title") or objective_id,
        }
    return lookup


def objective_label(objective_id: str, objective_lookup: dict[str, dict[str, str]]) -> str:
    metadata = objective_lookup.get(objective_id)
    if metadata is None:
        return f"{stable_code('OBJ', objective_id)} · {objective_id}"
    return f"{metadata['code']} · {metadata['title']}"


def activity_label(activity: dict[str, Any]) -> str:
    return f"{activity_code(activity)} · {activity.get('display_name') or activity['activity_id']} [attempt {activity.get('attempt', 1)}]"


def history_activity_label(entry: dict[str, Any]) -> str:
    return f"{activity_code(entry)} · {entry.get('display_name') or entry['activity_id']} [attempt {entry.get('attempt', 1)}]"


def activity_code(activity: dict[str, Any]) -> str:
    kind = activity.get("kind", "activity")
    prefix = {
        "task_execution": "TSK",
        "objective_plan": "OPL",
        "capability_plan": "CPL",
    }.get(kind, "ACT")
    return stable_code(prefix, str(activity["activity_id"]))


def stable_code(prefix: str, value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:6].upper()
    return f"{prefix}-{digest}"


def status_label(activity: dict[str, Any]) -> str:
    if activity.get("status_reason"):
        return f"{activity['status']} ({activity['status_reason']})"
    return activity["status"]


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_text(value: str | None) -> str:
    timestamp = parse_timestamp(value)
    if timestamp is None:
        return "-"
    delta = datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s ago"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m ago"


def elapsed_text(activity: dict[str, Any]) -> str:
    return age_text(activity.get("started_at"))


def last_event_age_text(activity: dict[str, Any]) -> str:
    latest_event = activity.get("latest_event") or {}
    return age_text(latest_event.get("timestamp") or activity.get("updated_at"))


def relative_timestamp(timestamp: str | None) -> str:
    return age_text(timestamp)
