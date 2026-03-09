from __future__ import annotations

from pathlib import Path
from typing import Any

from .filesystem import read_json, write_json
from .live import record_event
from .parallelism import execution_mode
from .schemas import validate_document
from .worktree_manager import WorktreeError, merge_task_branch


def assemble_review_bundle(
    project_root: Path,
    run_id: str,
    bundle_id: str,
    report_paths: list[Path],
    assembled_by: str,
    reviewed_by: str,
) -> dict[str, Any]:
    reports = [read_json(path) for path in report_paths]
    objective_id = reports[0]["objective_id"]
    phase = reports[0]["phase"]
    for report in reports:
        if report["objective_id"] != objective_id:
            raise ValueError("Review bundles may only contain reports from one objective")
        if report["phase"] != phase:
            raise ValueError("Review bundles may only contain reports from one phase")
    payload = {
        "schema": "review-bundle.v1",
        "run_id": run_id,
        "phase": phase,
        "objective_id": objective_id,
        "bundle_id": bundle_id,
        "assembled_by": assembled_by,
        "reviewed_by": reviewed_by,
        "included_tasks": [report["task_id"] for report in reports],
        "status": "pending_review",
        "required_checks": [
            "all reports are ready for bundle review",
            "all validation results passed",
            "all blocking collaboration requests are resolved",
        ],
    }
    validate_document(payload, "review-bundle.v1", project_root)
    bundle_path = project_root / "runs" / run_id / "bundles" / f"{bundle_id}.json"
    write_json(bundle_path, payload)
    record_event(
        project_root,
        run_id,
        phase=phase,
        activity_id=None,
        event_type="bundle.assembled",
        message=f"Assembled review bundle {bundle_id}.",
        payload={"bundle_id": bundle_id, "objective_id": objective_id, "included_tasks": payload["included_tasks"]},
    )
    return payload


def review_bundle(project_root: Path, run_id: str, bundle_id: str) -> dict[str, Any]:
    bundle_path = project_root / "runs" / run_id / "bundles" / f"{bundle_id}.json"
    bundle = read_json(bundle_path)
    record_event(
        project_root,
        run_id,
        phase=bundle["phase"],
        activity_id=None,
        event_type="bundle.review_started",
        message=f"Reviewing bundle {bundle_id}.",
        payload={"bundle_id": bundle_id, "objective_id": bundle["objective_id"]},
    )
    reports_dir = project_root / "runs" / run_id / "reports"
    collaboration_dir = project_root / "runs" / run_id / "collaboration"
    known_collaboration_ids = {path.stem for path in collaboration_dir.glob("*.json")}
    failures: list[str] = []

    for task_id in bundle["included_tasks"]:
        report = read_json(reports_dir / f"{task_id}.json")
        if report["status"] != "ready_for_bundle_review":
            failures.append(f"{task_id}: status is {report['status']}")
        for result in report.get("validation_results", []):
            if result["status"] != "passed":
                failures.append(f"{task_id}: validation {result['id']} did not pass")
        for request_id in report.get("follow_up_requests", []):
            if request_id not in known_collaboration_ids:
                continue
            request_path = collaboration_dir / f"{request_id}.json"
            if request_path.exists():
                request = read_json(request_path)
                if request.get("blocking") and request.get("status", "open") != "resolved":
                    failures.append(f"{task_id}: collaboration request {request_id} is still blocking")

    if failures:
        bundle["status"] = "rejected"
        bundle["rejection_reasons"] = failures
    else:
        bundle["status"] = "accepted"
        bundle["rejection_reasons"] = []
    write_json(bundle_path, bundle)
    record_event(
        project_root,
        run_id,
        phase=bundle["phase"],
        activity_id=None,
        event_type="bundle.accepted" if bundle["status"] == "accepted" else "bundle.rejected",
        message=f"Bundle {bundle_id} {bundle['status']}.",
        payload={"bundle_id": bundle_id, "rejection_reasons": bundle.get("rejection_reasons", [])},
    )
    return bundle


def land_accepted_bundle(project_root: Path, run_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
    run_dir = project_root / "runs" / run_id
    bundle_path = run_dir / "bundles" / f"{bundle['bundle_id']}.json"
    task_payloads = {
        task_id: read_json(run_dir / "tasks" / f"{task_id}.json")
        for task_id in bundle["included_tasks"]
    }
    isolated_tasks = [
        task_id
        for task_id in sorted(bundle["included_tasks"])
        if execution_mode(task_payloads[task_id]) == "isolated_write"
    ]
    if not isolated_tasks:
        return {"status": "accepted", "bundle": bundle, "landing_results": []}

    record_event(
        project_root,
        run_id,
        phase=bundle["phase"],
        activity_id=None,
        event_type="bundle.landing_started",
        message=f"Landing accepted bundle {bundle['bundle_id']}.",
        payload={"bundle_id": bundle["bundle_id"], "task_ids": isolated_tasks},
    )
    landing_results = []
    conflicts = []
    for task_id in isolated_tasks:
        try:
            result = merge_task_branch(project_root, run_id, task_id, bundle_id=bundle["bundle_id"])
        except WorktreeError as exc:
            result = {
                "status": "conflict",
                "branch_name": None,
                "workspace_path": None,
                "conflict_summary_path": None,
                "error": str(exc),
            }
        result["task_id"] = task_id
        landing_results.append(result)
        if result["status"] != "merged":
            conflicts.append(result)

    bundle["landing_results"] = landing_results
    if conflicts:
        bundle["status"] = "blocked"
        reasons = [
            f"{item['task_id']}: merge conflict while landing accepted work"
            for item in conflicts
        ]
        bundle["rejection_reasons"] = reasons
        write_json(bundle_path, bundle)
        record_event(
            project_root,
            run_id,
            phase=bundle["phase"],
            activity_id=None,
            event_type="bundle.merge_conflict",
            message=f"Accepted bundle {bundle['bundle_id']} hit a merge conflict.",
            payload={"bundle_id": bundle["bundle_id"], "conflicts": conflicts},
        )
        return {"status": "blocked", "bundle": bundle, "conflicts": conflicts}

    write_json(bundle_path, bundle)
    record_event(
        project_root,
        run_id,
        phase=bundle["phase"],
        activity_id=None,
        event_type="bundle.landed",
        message=f"Accepted bundle {bundle['bundle_id']} landed on the run integration branch.",
        payload={"bundle_id": bundle["bundle_id"], "landing_results": landing_results},
    )
    return {"status": "accepted", "bundle": bundle, "landing_results": landing_results}
