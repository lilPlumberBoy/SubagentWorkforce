from __future__ import annotations

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

from .filesystem import load_optional_json, read_text
from .live import list_activities, read_activity, read_activity_history, read_events, read_run_state

T = TypeVar("T")


def watch_run(project_root: Path, run_id: str, *, refresh_seconds: float = 1.0) -> None:
    console = Console()
    try:
        with Live(build_run_dashboard(project_root, run_id), console=console, refresh_per_second=4) as live:
            while True:
                time.sleep(refresh_seconds)
                live.update(build_run_dashboard(project_root, run_id), refresh=True)
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
    with Live(build_run_dashboard(project_root, run_id), console=console, refresh_per_second=4) as live:
        while not completed.wait(refresh_seconds):
            live.update(build_run_dashboard(project_root, run_id), refresh=True)
        live.update(build_run_dashboard(project_root, run_id), refresh=True)
    thread.join()
    if "value" in error:
        raise error["value"]
    return result["value"]


def build_run_dashboard(project_root: Path, run_id: str):
    run_state = read_run_state(project_root, run_id)
    current_phase = run_state["current_phase"]
    activities = list_activities(project_root, run_id, phase=current_phase)
    history = read_activity_history(project_root, run_id)
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

    renderables = [
        build_run_header(run_id, run_state),
        build_counts_table(run_state),
        build_objective_progress_table(objectives, activities, objective_lookup),
        build_activity_table("Active Planning Activities", active_plans, objective_lookup),
        build_activity_table("Active Task Activities", active_tasks, objective_lookup),
        build_activity_table("Queued Tasks", queued_tasks, objective_lookup),
        build_activity_table("Blocked Tasks", blocked_tasks, objective_lookup),
        build_activity_table("Interrupted / Recovered Activities", interrupted_activities, objective_lookup),
        build_warning_rollup_panel(activities, objective_lookup),
        build_recovery_rollup_panel(activities),
        build_phase_progress_panel(objectives, activities),
        build_activity_history_panel(history, objective_lookup),
    ]
    return Group(*renderables)


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


def build_run_header(run_id: str, run_state: dict[str, Any]) -> Panel:
    text = Text()
    text.append(f"Run: {run_id}\n", style="bold")
    text.append(f"Phase: {run_state['current_phase']}\n")
    text.append(f"Updated: {run_state['updated_at']}")
    return Panel(text, title="Run Status", border_style="green")


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
    table.add_column("Warnings")
    table.add_column("Progress")
    table.add_column("Current")
    if not activities:
        table.add_row("none", "-", "-", "-", progress_renderable(0.0), "-")
        return table
    for activity in sorted(activities, key=activity_sort_key):
        warnings_text = "; ".join(item["message"] for item in activity.get("warnings", [])) or "-"
        table.add_row(
            activity_label(activity),
            objective_label(activity["objective_id"], objective_lookup),
            status_label(activity),
            warnings_text,
            progress_renderable(activity["progress_fraction"]),
            activity.get("current_activity") or "-",
        )
    return table


def build_activity_summary_panel(activity: dict[str, Any]) -> Panel:
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
        f"Status reason: {activity.get('status_reason') or '-'}",
        f"Recovery action: {activity.get('recovery_action') or '-'}",
        f"Interrupted at: {activity.get('interrupted_at') or '-'}",
        f"Recovered at: {activity.get('recovered_at') or '-'}",
        f"Parallel requested: {activity.get('parallel_execution_requested', False)}",
        f"Parallel granted: {activity.get('parallel_execution_granted', False)}",
        f"Fallback reason: {activity.get('parallel_fallback_reason') or '-'}",
        f"Workspace: {activity.get('workspace_path') or '-'}",
        f"Branch: {activity.get('branch_name') or '-'}",
        f"Current: {activity.get('current_activity') or '-'}",
        f"Updated: {activity['updated_at']}",
    ]
    warnings = activity.get("warnings", [])
    if warnings:
        lines.append("Warnings:")
        for item in warnings:
            lines.append(f"- {item['code']}: {item['message']}")
    return Panel("\n".join(lines), title="Activity", border_style="yellow")


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
        return "No prompt path recorded."
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
