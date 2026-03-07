from __future__ import annotations

from pathlib import Path
from typing import Any

from .filesystem import read_json, write_json
from .schemas import validate_document


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
    return payload


def review_bundle(project_root: Path, run_id: str, bundle_id: str) -> dict[str, Any]:
    bundle_path = project_root / "runs" / run_id / "bundles" / f"{bundle_id}.json"
    bundle = read_json(bundle_path)
    reports_dir = project_root / "runs" / run_id / "reports"
    collaboration_dir = project_root / "runs" / run_id / "collaboration"
    failures: list[str] = []

    for task_id in bundle["included_tasks"]:
        report = read_json(reports_dir / f"{task_id}.json")
        if report["status"] != "ready_for_bundle_review":
            failures.append(f"{task_id}: status is {report['status']}")
        for result in report.get("validation_results", []):
            if result["status"] != "passed":
                failures.append(f"{task_id}: validation {result['id']} did not pass")
        for request_id in report.get("follow_up_requests", []):
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
    return bundle
