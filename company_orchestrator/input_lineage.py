from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
from typing import Any

from .filesystem import read_json
from .handoffs import list_handoffs, normalize_handoff_payload
from .output_descriptors import descriptor_output_id, descriptor_path, normalize_output_descriptors


TASK_OUTPUT_REF_PATTERN = re.compile(r"^Outputs? from ([A-Za-z0-9._:-]+)$|^Output of ([A-Za-z0-9._:-]+)$")


def build_task_input_source_metadata(
    project_root: Path,
    run_id: str,
    task: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    run_dir = project_root / "runs" / run_id
    tasks_by_id: dict[str, dict[str, Any]] = {}
    tasks_dir = run_dir / "tasks"
    if tasks_dir.exists():
        for path in sorted(tasks_dir.glob("*.json")):
            payload = read_json(path)
            tasks_by_id[str(payload.get("task_id", ""))] = payload

    output_records_by_task_id: dict[str, list[dict[str, str]]] = {}
    output_records_by_path: dict[str, list[dict[str, str]]] = defaultdict(list)
    for task_id, producer_task in tasks_by_id.items():
        output_records = producer_output_records(project_root, run_id, producer_task)
        output_records_by_task_id[task_id] = output_records
        for record in output_records:
            artifact_path = record.get("path")
            if artifact_path:
                output_records_by_path[artifact_path].append(record)

    handoffs_by_id: dict[str, dict[str, Any]] = {}
    handoff_records_by_path: dict[str, list[dict[str, str]]] = defaultdict(list)
    handoff_records_by_task_id: dict[str, list[dict[str, str]]] = defaultdict(list)
    for payload in list_handoffs(run_dir):
        handoff = normalize_handoff_payload(payload)
        handoffs_by_id[handoff["handoff_id"]] = handoff
        for record in handoff_deliverable_records(handoff):
            handoff_records_by_task_id[record["from_task_id"]].append(record)
            artifact_path = record.get("path")
            if artifact_path:
                handoff_records_by_path[artifact_path].append(record)

    current_task_id = str(task.get("task_id", "")).strip()
    handoff_dependencies = {
        str(value).strip()
        for value in task.get("handoff_dependencies", [])
        if isinstance(value, str) and str(value).strip()
    }
    metadata: dict[str, dict[str, Any]] = {}

    for raw_ref in task.get("inputs", []):
        if not isinstance(raw_ref, str):
            continue
        input_ref = raw_ref.strip()
        if not input_ref:
            continue
        output_records: list[dict[str, str]] = []
        handoff_records: list[dict[str, str]] = []
        referenced_task_id = referenced_task_output_id(input_ref)
        if referenced_task_id is not None:
            output_records.extend(output_records_by_task_id.get(referenced_task_id, []))
            handoff_records.extend(
                filter_relevant_handoffs(
                    handoff_records_by_task_id.get(referenced_task_id, []),
                    current_task_id=current_task_id,
                    allowed_handoff_ids=handoff_dependencies,
                )
            )
        else:
            output_records.extend(output_records_by_path.get(input_ref, []))
            handoff_records.extend(
                filter_relevant_handoffs(
                    handoff_records_by_path.get(input_ref, []),
                    current_task_id=current_task_id,
                    allowed_handoff_ids=handoff_dependencies,
                )
            )
        metadata[input_ref] = summarize_input_source_metadata(input_ref, output_records, handoff_records)

    for handoff_id in sorted(handoff_dependencies):
        handoff = handoffs_by_id.get(handoff_id)
        handoff_records = handoff_deliverable_records(handoff) if handoff is not None else []
        metadata[handoff_id] = summarize_input_source_metadata(handoff_id, [], handoff_records)

    return metadata


def referenced_task_output_id(input_ref: str) -> str | None:
    match = TASK_OUTPUT_REF_PATTERN.match(str(input_ref or "").strip())
    if match is None:
        return None
    task_id = match.group(1) or match.group(2)
    if task_id is None:
        return None
    normalized = task_id.strip()
    return normalized or None


def producer_output_records(project_root: Path, run_id: str, task: dict[str, Any]) -> list[dict[str, str]]:
    task_id = str(task.get("task_id", "")).strip()
    objective_id = str(task.get("objective_id", "")).strip()
    phase = str(task.get("phase", "")).strip()
    report_path = project_root / "runs" / run_id / "reports" / f"{task_id}.json"
    output_source = read_json(report_path).get("produced_outputs", []) if report_path.exists() else task.get("expected_outputs", [])
    records: list[dict[str, str]] = []
    for descriptor in normalize_output_descriptors(list(output_source), allow_legacy_strings=False):
        record = {
            "task_id": task_id,
            "objective_id": objective_id,
            "phase": phase,
            "output_id": descriptor_output_id(descriptor),
        }
        artifact_path = descriptor_path(descriptor)
        if artifact_path:
            record["path"] = artifact_path
        records.append(record)
    return records


def handoff_deliverable_records(handoff: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(handoff, dict):
        return []
    handoff_id = str(handoff.get("handoff_id", "")).strip()
    from_task_id = str(handoff.get("from_task_id", "")).strip()
    objective_id = str(handoff.get("objective_id", "")).strip()
    phase = str(handoff.get("phase", "")).strip()
    target_task_ids = [
        str(value).strip()
        for value in handoff.get("to_task_ids", [])
        if isinstance(value, str) and str(value).strip()
    ]
    records: list[dict[str, str]] = []
    for descriptor in normalize_output_descriptors(list(handoff.get("deliverables", [])), allow_legacy_strings=False):
        record = {
            "handoff_id": handoff_id,
            "from_task_id": from_task_id,
            "objective_id": objective_id,
            "phase": phase,
            "output_id": descriptor_output_id(descriptor),
        }
        artifact_path = descriptor_path(descriptor)
        if artifact_path:
            record["path"] = artifact_path
        if target_task_ids:
            record["to_task_ids"] = ",".join(target_task_ids)
        records.append(record)
    return records


def filter_relevant_handoffs(
    records: list[dict[str, str]],
    *,
    current_task_id: str,
    allowed_handoff_ids: set[str],
) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    for record in records:
        handoff_id = record.get("handoff_id", "")
        target_ids = {
            value.strip()
            for value in str(record.get("to_task_ids", "")).split(",")
            if value.strip()
        }
        if handoff_id in allowed_handoff_ids or not target_ids or current_task_id in target_ids:
            filtered.append(record)
    return filtered


def summarize_input_source_metadata(
    input_ref: str,
    output_records: list[dict[str, str]],
    handoff_records: list[dict[str, str]],
) -> dict[str, Any]:
    output_ids = unique_strings(record.get("output_id") for record in output_records + handoff_records)
    handoff_ids = unique_strings(record.get("handoff_id") for record in handoff_records)
    source_task_ids = unique_strings(record.get("task_id") for record in output_records)
    source_task_ids.extend(
        value for value in unique_strings(record.get("from_task_id") for record in handoff_records) if value not in source_task_ids
    )
    source_objective_ids = unique_strings(record.get("objective_id") for record in output_records + handoff_records)
    artifact_paths = unique_strings(record.get("path") for record in output_records + handoff_records)
    summary = {
        "input_ref": input_ref,
        "resolved": bool(output_ids or handoff_ids or source_task_ids),
        "source_task_ids": source_task_ids,
        "source_objective_ids": source_objective_ids,
        "output_ids": output_ids,
        "handoff_ids": handoff_ids,
        "artifact_paths": artifact_paths,
    }
    return summary


def unique_strings(values: Any) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized
