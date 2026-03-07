from __future__ import annotations

from pathlib import Path
from typing import Any

from .filesystem import read_json, write_json
from .schemas import validate_document


def create_collaboration_request(
    project_root: Path,
    run_id: str,
    request_id: str,
    objective_id: str,
    from_role: str,
    to_role: str,
    request_type: str,
    summary: str,
    blocking: bool = True,
) -> dict[str, Any]:
    payload = {
        "schema": "collaboration-request.v1",
        "run_id": run_id,
        "request_id": request_id,
        "objective_id": objective_id,
        "from_role": from_role,
        "to_role": to_role,
        "type": request_type,
        "summary": summary,
        "blocking": blocking,
        "status": "open",
    }
    validate_document(payload, "collaboration-request.v1", project_root)
    path = project_root / "runs" / run_id / "collaboration" / f"{request_id}.json"
    write_json(path, payload)
    return payload


def resolve_collaboration_request(project_root: Path, run_id: str, request_id: str) -> dict[str, Any]:
    path = project_root / "runs" / run_id / "collaboration" / f"{request_id}.json"
    payload = read_json(path)
    payload["status"] = "resolved"
    write_json(path, payload)
    return payload
