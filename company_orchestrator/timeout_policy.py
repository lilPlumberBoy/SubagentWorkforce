from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimeoutPolicy:
    timeout_seconds: int
    max_timeout_retries: int
    source: str


PLANNING_TIMEOUTS = {
    "discovery": TimeoutPolicy(timeout_seconds=600, max_timeout_retries=1, source="policy"),
    "design": TimeoutPolicy(timeout_seconds=750, max_timeout_retries=1, source="policy"),
    "mvp-build": TimeoutPolicy(timeout_seconds=600, max_timeout_retries=1, source="policy"),
    "polish": TimeoutPolicy(timeout_seconds=450, max_timeout_retries=1, source="policy"),
}

TASK_TIMEOUTS = {
    ("discovery", "read_only"): TimeoutPolicy(timeout_seconds=300, max_timeout_retries=1, source="policy"),
    ("discovery", "isolated_write"): TimeoutPolicy(timeout_seconds=450, max_timeout_retries=1, source="policy"),
    ("design", "read_only"): TimeoutPolicy(timeout_seconds=450, max_timeout_retries=1, source="policy"),
    ("design", "isolated_write"): TimeoutPolicy(timeout_seconds=600, max_timeout_retries=1, source="policy"),
    ("mvp-build", "read_only"): TimeoutPolicy(timeout_seconds=450, max_timeout_retries=1, source="policy"),
    ("mvp-build", "isolated_write"): TimeoutPolicy(timeout_seconds=600, max_timeout_retries=1, source="policy"),
    ("polish", "read_only"): TimeoutPolicy(timeout_seconds=450, max_timeout_retries=1, source="policy"),
    ("polish", "isolated_write"): TimeoutPolicy(timeout_seconds=600, max_timeout_retries=1, source="policy"),
}


def resolve_planning_timeout_policy(phase: str, timeout_seconds: int | None) -> TimeoutPolicy:
    if timeout_seconds is not None:
        return TimeoutPolicy(timeout_seconds=max(1, int(timeout_seconds)), max_timeout_retries=0, source="explicit")
    return PLANNING_TIMEOUTS.get(phase, PLANNING_TIMEOUTS["design"])


def resolve_task_timeout_policy(phase: str, execution_mode: str, timeout_seconds: int | None) -> TimeoutPolicy:
    if timeout_seconds is not None:
        return TimeoutPolicy(timeout_seconds=max(1, int(timeout_seconds)), max_timeout_retries=0, source="explicit")
    key = (phase, execution_mode or "read_only")
    return TASK_TIMEOUTS.get(key, TASK_TIMEOUTS[(phase, "read_only")] if (phase, "read_only") in TASK_TIMEOUTS else TASK_TIMEOUTS[("design", "read_only")])


def timeout_retry_message(kind: str, label: str, *, timeout_seconds: int, attempt: int, max_attempts: int) -> str:
    return (
        f"{kind.title()} {label} timed out after {timeout_seconds} seconds on attempt "
        f"{attempt}/{max_attempts}. Retrying."
    )


def timeout_final_message(
    kind: str,
    label: str,
    *,
    timeout_seconds: int,
    attempts: int,
    resume_recommended: bool,
    explicit_override: bool,
) -> str:
    if kind == "planning":
        base = f"codex exec timed out after {timeout_seconds} seconds while planning {label}"
    else:
        base = f"codex exec timed out after {timeout_seconds} seconds for task {label}"
    recommendation = "resume-phase is recommended" if resume_recommended else "retry-activity is recommended"
    if explicit_override:
        recommendation = "no automatic retry was attempted because timeout_seconds was explicitly set"
    if attempts <= 1:
        return f"{base}; {recommendation}."
    return f"{base} after {attempts} attempts; {recommendation}."
