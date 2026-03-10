from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from .bundles import assemble_review_bundle, review_bundle
from .changes import analyze_change_request, approve_change, create_change_request, scaffold_delta_run
from .collaboration import create_collaboration_request, resolve_collaboration_request
from .executor import execute_task
from .filesystem import read_json
from .management import run_objective, run_phase
from .monitoring import inspect_activity, run_with_watch, watch_run
from .objective_planner import plan_objective, plan_phase
from .planner import decompose_goal, generate_role_files, initialize_run, promote_roles, suggest_team_proposals
from .prompts import render_prompt
from .recovery import reconcile_run
from .reports import advance_phase, generate_phase_report, record_human_approval
from .schemas import validate_document
from .smoke import scaffold_smoke_test, simulate_context_echo_completion, verify_smoke_reports


def main() -> None:
    parser = argparse.ArgumentParser(prog="company-orchestrator")
    parser.add_argument("--project-root", default=".")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init-run")
    init_parser.add_argument("run_id")
    init_parser.add_argument("goal_file")

    subparsers.add_parser("decompose-goal").add_argument("run_id")
    subparsers.add_parser("suggest-teams").add_argument("run_id")

    roles_parser = subparsers.add_parser("generate-roles")
    roles_parser.add_argument("run_id")
    roles_parser.add_argument("--approve", action="store_true")

    promote_parser = subparsers.add_parser("promote-roles")
    promote_parser.add_argument("objective_id")

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("schema_name")
    validate_parser.add_argument("document_path")

    render_parser = subparsers.add_parser("render-prompt")
    render_parser.add_argument("run_id")
    render_parser.add_argument("task_path")

    execute_parser = subparsers.add_parser("execute-task")
    execute_parser.add_argument("run_id")
    execute_parser.add_argument("task_id")
    execute_parser.add_argument("--sandbox", default="read-only")
    execute_parser.add_argument("--codex-path", default="codex")
    execute_parser.add_argument("--timeout-seconds", type=int, default=300)
    execute_parser.add_argument("--watch", action="store_true")
    execute_parser.add_argument("--watch-refresh-seconds", type=float, default=1.0)

    plan_objective_parser = subparsers.add_parser("plan-objective")
    plan_objective_parser.add_argument("run_id")
    plan_objective_parser.add_argument("objective_id")
    plan_objective_parser.add_argument("--sandbox", default="read-only")
    plan_objective_parser.add_argument("--codex-path", default="codex")
    plan_objective_parser.add_argument("--replace", action="store_true")
    plan_objective_parser.add_argument("--timeout-seconds", type=int, default=300)
    plan_objective_parser.add_argument("--watch", action="store_true")
    plan_objective_parser.add_argument("--watch-refresh-seconds", type=float, default=1.0)

    plan_phase_parser = subparsers.add_parser("plan-phase")
    plan_phase_parser.add_argument("run_id")
    plan_phase_parser.add_argument("--sandbox", default="read-only")
    plan_phase_parser.add_argument("--codex-path", default="codex")
    plan_phase_parser.add_argument("--replace", action="store_true")
    plan_phase_parser.add_argument("--timeout-seconds", type=int, default=300)
    plan_phase_parser.add_argument("--watch", action="store_true")
    plan_phase_parser.add_argument("--watch-refresh-seconds", type=float, default=1.0)

    run_objective_parser = subparsers.add_parser("run-objective")
    run_objective_parser.add_argument("run_id")
    run_objective_parser.add_argument("objective_id")
    run_objective_parser.add_argument("--sandbox", default="read-only")
    run_objective_parser.add_argument("--codex-path", default="codex")
    run_objective_parser.add_argument("--force", action="store_true")
    run_objective_parser.add_argument("--timeout-seconds", type=int, default=300)
    run_objective_parser.add_argument("--max-concurrency", type=int, default=3)
    run_objective_parser.add_argument("--watch", action="store_true")
    run_objective_parser.add_argument("--watch-refresh-seconds", type=float, default=1.0)

    run_phase_parser = subparsers.add_parser("run-phase")
    run_phase_parser.add_argument("run_id")
    run_phase_parser.add_argument("--sandbox", default="read-only")
    run_phase_parser.add_argument("--codex-path", default="codex")
    run_phase_parser.add_argument("--force", action="store_true")
    run_phase_parser.add_argument("--timeout-seconds", type=int, default=300)
    run_phase_parser.add_argument("--max-concurrency", type=int, default=3)
    run_phase_parser.add_argument("--watch", action="store_true")
    run_phase_parser.add_argument("--watch-refresh-seconds", type=float, default=1.0)

    watch_parser = subparsers.add_parser("watch-run")
    watch_parser.add_argument("run_id")
    watch_parser.add_argument("--refresh-seconds", type=float, default=1.0)

    inspect_parser = subparsers.add_parser("inspect-activity")
    inspect_parser.add_argument("run_id")
    inspect_parser.add_argument("activity_id")
    inspect_parser.add_argument("--follow", action="store_true")
    inspect_parser.add_argument("--events", type=int, default=20)

    reconcile_parser = subparsers.add_parser("reconcile-run")
    reconcile_parser.add_argument("run_id")
    reconcile_parser.add_argument("--apply", action="store_true")

    resume_parser = subparsers.add_parser("resume-phase")
    resume_parser.add_argument("run_id")
    resume_parser.add_argument("--sandbox", default="read-only")
    resume_parser.add_argument("--codex-path", default="codex")
    resume_parser.add_argument("--force", action="store_true")
    resume_parser.add_argument("--timeout-seconds", type=int, default=300)
    resume_parser.add_argument("--max-concurrency", type=int, default=3)
    resume_parser.add_argument("--watch", action="store_true")
    resume_parser.add_argument("--watch-refresh-seconds", type=float, default=1.0)

    retry_parser = subparsers.add_parser("retry-activity")
    retry_parser.add_argument("run_id")
    retry_parser.add_argument("activity_id")
    retry_parser.add_argument("--sandbox", default="read-only")
    retry_parser.add_argument("--codex-path", default="codex")
    retry_parser.add_argument("--timeout-seconds", type=int, default=300)
    retry_parser.add_argument("--watch", action="store_true")
    retry_parser.add_argument("--watch-refresh-seconds", type=float, default=1.0)

    bundle_parser = subparsers.add_parser("assemble-bundle")
    bundle_parser.add_argument("run_id")
    bundle_parser.add_argument("bundle_id")
    bundle_parser.add_argument("assembled_by")
    bundle_parser.add_argument("reviewed_by")
    bundle_parser.add_argument("report_paths", nargs="+")

    review_parser = subparsers.add_parser("review-bundle")
    review_parser.add_argument("run_id")
    review_parser.add_argument("bundle_id")

    collaboration_parser = subparsers.add_parser("create-collaboration")
    collaboration_parser.add_argument("run_id")
    collaboration_parser.add_argument("request_id")
    collaboration_parser.add_argument("objective_id")
    collaboration_parser.add_argument("from_role")
    collaboration_parser.add_argument("to_role")
    collaboration_parser.add_argument("request_type")
    collaboration_parser.add_argument("summary")
    collaboration_parser.add_argument("--non-blocking", action="store_true")

    resolve_collaboration_parser = subparsers.add_parser("resolve-collaboration")
    resolve_collaboration_parser.add_argument("run_id")
    resolve_collaboration_parser.add_argument("request_id")

    report_parser = subparsers.add_parser("phase-report")
    report_parser.add_argument("run_id")

    approve_phase_parser = subparsers.add_parser("approve-phase")
    approve_phase_parser.add_argument("run_id")
    approve_phase_parser.add_argument("phase")

    advance_parser = subparsers.add_parser("advance-phase")
    advance_parser.add_argument("run_id")

    smoke_parser = subparsers.add_parser("scaffold-smoke-test")
    smoke_parser.add_argument("--run-id", default="smoke-demo")

    simulate_parser = subparsers.add_parser("simulate-context-echo")
    simulate_parser.add_argument("run_id")
    simulate_parser.add_argument("task_id")

    verify_smoke_parser = subparsers.add_parser("verify-smoke")
    verify_smoke_parser.add_argument("run_id")

    change_parser = subparsers.add_parser("create-change")
    change_parser.add_argument("run_id")
    change_parser.add_argument("change_id")
    change_parser.add_argument("summary")
    change_parser.add_argument("--goal-changed", action="store_true")
    change_parser.add_argument("--scope-changed", action="store_true")
    change_parser.add_argument("--boundary-changed", action="store_true")
    change_parser.add_argument("--interface-changed", action="store_true")
    change_parser.add_argument("--architecture-changed", action="store_true")
    change_parser.add_argument("--team-changed", action="store_true")
    change_parser.add_argument("--implementation-changed", action="store_true")

    analyze_parser = subparsers.add_parser("analyze-change")
    analyze_parser.add_argument("run_id")
    analyze_parser.add_argument("change_id")

    approve_change_parser = subparsers.add_parser("approve-change")
    approve_change_parser.add_argument("run_id")
    approve_change_parser.add_argument("change_id")

    delta_parser = subparsers.add_parser("scaffold-delta")
    delta_parser.add_argument("run_id")
    delta_parser.add_argument("change_id")

    args = parser.parse_args()
    project_root = Path(args.project_root).resolve()

    if args.command == "init-run":
        goal_text = Path(args.goal_file).read_text(encoding="utf-8")
        result = initialize_run(project_root, args.run_id, goal_text)
        print(result)
        return
    if args.command == "decompose-goal":
        print_json(decompose_goal(project_root, args.run_id))
        return
    if args.command == "suggest-teams":
        print_json(suggest_team_proposals(project_root, args.run_id))
        return
    if args.command == "generate-roles":
        print_json({"written": [str(path) for path in generate_role_files(project_root, args.run_id, args.approve)]})
        return
    if args.command == "promote-roles":
        print_json({"written": [str(path) for path in promote_roles(project_root, args.objective_id)]})
        return
    if args.command == "validate":
        payload = read_json(Path(args.document_path))
        validate_document(payload, args.schema_name, project_root)
        print_json({"status": "ok"})
        return
    if args.command == "render-prompt":
        print_json(render_prompt(project_root, args.run_id, Path(args.task_path)))
        return
    if args.command == "execute-task":
        operation = lambda: execute_task(
                project_root,
                args.run_id,
                args.task_id,
                sandbox_mode=args.sandbox,
                codex_path=args.codex_path,
                timeout_seconds=args.timeout_seconds,
            )
        print_result(
            run_maybe_watched(project_root, args.run_id, args.watch, args.watch_refresh_seconds, operation),
            leading_blank_line=args.watch,
        )
        return
    if args.command == "plan-objective":
        operation = lambda: plan_objective(
                project_root,
                args.run_id,
                args.objective_id,
                sandbox_mode=args.sandbox,
                codex_path=args.codex_path,
                replace=args.replace,
                timeout_seconds=args.timeout_seconds,
            )
        print_result(
            run_maybe_watched(project_root, args.run_id, args.watch, args.watch_refresh_seconds, operation),
            leading_blank_line=args.watch,
        )
        return
    if args.command == "plan-phase":
        operation = lambda: plan_phase(
                project_root,
                args.run_id,
                sandbox_mode=args.sandbox,
                codex_path=args.codex_path,
                replace=args.replace,
                timeout_seconds=args.timeout_seconds,
            )
        print_result(
            run_maybe_watched(project_root, args.run_id, args.watch, args.watch_refresh_seconds, operation),
            leading_blank_line=args.watch,
        )
        return
    if args.command == "run-objective":
        operation = lambda: run_objective(
                project_root,
                args.run_id,
                args.objective_id,
                sandbox_mode=args.sandbox,
                codex_path=args.codex_path,
                force=args.force,
                timeout_seconds=args.timeout_seconds,
                max_concurrency=args.max_concurrency,
            )
        print_result(
            run_maybe_watched(project_root, args.run_id, args.watch, args.watch_refresh_seconds, operation),
            leading_blank_line=args.watch,
        )
        return
    if args.command == "run-phase":
        operation = lambda: run_phase(
                project_root,
                args.run_id,
                sandbox_mode=args.sandbox,
                codex_path=args.codex_path,
                force=args.force,
                timeout_seconds=args.timeout_seconds,
                max_concurrency=args.max_concurrency,
            )
        print_result(
            run_maybe_watched(project_root, args.run_id, args.watch, args.watch_refresh_seconds, operation),
            leading_blank_line=args.watch,
        )
        return
    if args.command == "watch-run":
        watch_run(project_root, args.run_id, refresh_seconds=args.refresh_seconds)
        return
    if args.command == "inspect-activity":
        inspect_activity(project_root, args.run_id, args.activity_id, follow=args.follow, events=args.events)
        return
    if args.command == "reconcile-run":
        print_json(reconcile_run(project_root, args.run_id, apply=args.apply))
        return
    if args.command == "resume-phase":
        operation = lambda: run_phase(
                project_root,
                args.run_id,
                sandbox_mode=args.sandbox,
                codex_path=args.codex_path,
                force=args.force,
                timeout_seconds=args.timeout_seconds,
                max_concurrency=args.max_concurrency,
            )
        print_result(
            run_maybe_watched(project_root, args.run_id, args.watch, args.watch_refresh_seconds, operation),
            leading_blank_line=args.watch,
        )
        return
    if args.command == "retry-activity":
        operation = lambda: retry_activity(
                project_root,
                args.run_id,
                args.activity_id,
                sandbox_mode=args.sandbox,
                codex_path=args.codex_path,
                timeout_seconds=args.timeout_seconds,
            )
        print_result(
            run_maybe_watched(project_root, args.run_id, args.watch, args.watch_refresh_seconds, operation),
            leading_blank_line=args.watch,
        )
        return
    if args.command == "assemble-bundle":
        report_paths = [Path(path) for path in args.report_paths]
        print_json(
            assemble_review_bundle(
                project_root, args.run_id, args.bundle_id, report_paths, args.assembled_by, args.reviewed_by
            )
        )
        return
    if args.command == "review-bundle":
        print_json(review_bundle(project_root, args.run_id, args.bundle_id))
        return
    if args.command == "create-collaboration":
        print_json(
            create_collaboration_request(
                project_root,
                args.run_id,
                args.request_id,
                args.objective_id,
                args.from_role,
                args.to_role,
                args.request_type,
                args.summary,
                blocking=not args.non_blocking,
            )
        )
        return
    if args.command == "resolve-collaboration":
        print_json(resolve_collaboration_request(project_root, args.run_id, args.request_id))
        return
    if args.command == "phase-report":
        report, _ = generate_phase_report(project_root, args.run_id)
        print_json(report)
        return
    if args.command == "approve-phase":
        print_json(record_human_approval(project_root, args.run_id, args.phase, True))
        return
    if args.command == "advance-phase":
        print_json(advance_phase(project_root, args.run_id))
        return
    if args.command == "scaffold-smoke-test":
        print(scaffold_smoke_test(project_root, args.run_id))
        return
    if args.command == "simulate-context-echo":
        print_json(simulate_context_echo_completion(project_root, args.run_id, args.task_id))
        return
    if args.command == "verify-smoke":
        print_json(verify_smoke_reports(project_root, args.run_id))
        return
    if args.command == "create-change":
        impact = {
            "goal_changed": args.goal_changed,
            "scope_changed": args.scope_changed,
            "boundary_changed": args.boundary_changed,
            "interface_changed": args.interface_changed,
            "architecture_changed": args.architecture_changed,
            "team_changed": args.team_changed,
            "implementation_changed": args.implementation_changed,
        }
        print_json(create_change_request(project_root, args.run_id, args.change_id, args.summary, impact))
        return
    if args.command == "analyze-change":
        print_json(analyze_change_request(project_root, args.run_id, args.change_id))
        return
    if args.command == "approve-change":
        print_json(approve_change(project_root, args.run_id, args.change_id, True))
        return
    if args.command == "scaffold-delta":
        print(scaffold_delta_run(project_root, args.run_id, args.change_id))
        return
    raise ValueError(f"Unknown command {args.command}")


def print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def print_result(payload: dict[str, object], *, leading_blank_line: bool = False) -> None:
    if leading_blank_line:
        print()
    print_json(payload)


def run_maybe_watched(
    project_root: Path,
    run_id: str,
    watch: bool,
    refresh_seconds: float,
    operation: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    if not watch:
        return operation()
    return run_with_watch(project_root, run_id, operation, refresh_seconds=refresh_seconds)


def retry_activity(
    project_root: Path,
    run_id: str,
    activity_id: str,
    *,
    sandbox_mode: str,
    codex_path: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    activity = read_json(project_root / "runs" / run_id / "live" / "activities" / f"{activity_id.replace(':', '__')}.json")
    if activity["kind"] == "task_execution":
        return execute_task(
            project_root,
            run_id,
            activity["activity_id"],
            sandbox_mode=sandbox_mode,
            codex_path=codex_path,
            timeout_seconds=timeout_seconds,
            allow_recovery_blocked=True,
        )
    return plan_objective(
        project_root,
        run_id,
        activity["objective_id"],
        sandbox_mode=sandbox_mode,
        codex_path=codex_path,
        replace=False,
        timeout_seconds=timeout_seconds,
        allow_recovery_blocked=True,
    )
