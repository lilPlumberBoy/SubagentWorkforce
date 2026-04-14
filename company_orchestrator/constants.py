from __future__ import annotations

from pathlib import Path

PHASES = ["discovery", "design", "mvp-build", "polish"]
ROLE_KINDS = {"manager", "worker", "acceptance-manager"}
SCHEMA_NAMES = {
    "phase-plan.v1": "phase-plan.v1.json",
    "objective-map.v1": "objective-map.v1.json",
    "team-registry.v1": "team-registry.v1.json",
    "task-assignment.v1": "task-assignment.v1.json",
    "completion-report.v1": "completion-report.v1.json",
    "review-bundle.v1": "review-bundle.v1.json",
    "collaboration-request.v1": "collaboration-request.v1.json",
    "phase-report.v1": "phase-report.v1.json",
    "change-request.v2": "change-request.v2.json",
    "change-impact.v1": "change-impact.v1.json",
    "user-feedback.v1": "user-feedback.v1.json",
    "executor-response.v1": "executor-response.v1.json",
    "objective-outline.v1": "objective-outline.v1.json",
    "objective-plan.v1": "objective-plan.v1.json",
    "capability-plan.v1": "capability-plan.v1.json",
    "collaboration-handoff.v1": "collaboration-handoff.v1.json",
    "output-descriptor.v1": "output-descriptor.v1.json",
    "run-live-state.v1": "run-live-state.v1.json",
    "activity-live-state.v1": "activity-live-state.v1.json",
    "live-event.v1": "live-event.v1.json",
    "llm-call.v1": "llm-call.v1.json",
    "run-observability.v1": "run-observability.v1.json",
    "autonomy-state.v1": "autonomy-state.v1.json",
    "autonomy-audit.v1": "autonomy-audit.v1.json",
}


def project_root(start: str | Path | None = None) -> Path:
    if start is not None:
        return Path(start).resolve()
    return Path(__file__).resolve().parent.parent


def orchestrator_root(start: str | Path | None = None) -> Path:
    return project_root(start) / "orchestrator"


def schema_root(start: str | Path | None = None) -> Path:
    return orchestrator_root(start) / "schemas"
