from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .filesystem import ensure_dir, write_json


class WorktreeError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceInfo:
    branch_name: str
    workspace_path: Path


def sanitize_ref_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return normalized or "unnamed"


def integration_branch_name(run_id: str) -> str:
    return f"codex/run-{sanitize_ref_component(run_id)}"


def task_branch_name(run_id: str, task_id: str) -> str:
    return f"codex/task-{sanitize_ref_component(run_id)}-{sanitize_ref_component(task_id)}"


def integration_workspace_path(project_root: Path, run_id: str) -> Path:
    return project_root / ".orchestrator-worktrees" / run_id / "integration"


def task_workspace_path(project_root: Path, run_id: str, task_id: str) -> Path:
    return project_root / ".orchestrator-worktrees" / run_id / "tasks" / task_id


def git_root(project_root: Path) -> Path:
    completed = git(project_root, ["rev-parse", "--show-toplevel"])
    return Path(completed.stdout.strip()).resolve()


def ensure_run_integration_workspace(project_root: Path, run_id: str) -> WorkspaceInfo:
    repo_root = git_root(project_root)
    branch_name = integration_branch_name(run_id)
    workspace_path = integration_workspace_path(project_root, run_id)
    if not branch_exists(repo_root, branch_name):
        git(repo_root, ["branch", branch_name, "HEAD"])
    ensure_worktree(repo_root, branch_name, workspace_path)
    return WorkspaceInfo(branch_name=branch_name, workspace_path=workspace_path)


def ensure_task_workspace(project_root: Path, run_id: str, task_id: str) -> WorkspaceInfo:
    repo_root = git_root(project_root)
    integration = ensure_run_integration_workspace(project_root, run_id)
    branch_name = task_branch_name(run_id, task_id)
    workspace_path = task_workspace_path(project_root, run_id, task_id)
    if not branch_exists(repo_root, branch_name):
        git(repo_root, ["branch", branch_name, integration.branch_name])
    ensure_worktree(repo_root, branch_name, workspace_path)
    return WorkspaceInfo(branch_name=branch_name, workspace_path=workspace_path)


def commit_task_workspace(workspace: WorkspaceInfo, task_id: str) -> dict[str, Any]:
    git(workspace.workspace_path, ["add", "-A"])
    status = git(workspace.workspace_path, ["status", "--porcelain"])
    if not status.stdout.strip():
        return {"committed": False, "commit_sha": None}
    git(
        workspace.workspace_path,
        [
            "-c",
            "user.name=company-orchestrator",
            "-c",
            "user.email=company-orchestrator@example.invalid",
            "commit",
            "-m",
            f"orchestrator: {task_id}",
        ],
    )
    head = git(workspace.workspace_path, ["rev-parse", "HEAD"]).stdout.strip()
    return {"committed": True, "commit_sha": head}


def merge_task_branch(
    project_root: Path,
    run_id: str,
    task_id: str,
    *,
    bundle_id: str,
) -> dict[str, Any]:
    integration = ensure_run_integration_workspace(project_root, run_id)
    branch_name = task_branch_name(run_id, task_id)
    completed = git(
        integration.workspace_path,
        ["merge", "--no-ff", "--no-edit", branch_name],
        check=False,
    )
    if completed.returncode == 0:
        return {
            "status": "merged",
            "branch_name": branch_name,
            "workspace_path": str(integration.workspace_path.relative_to(project_root)),
            "conflict_summary_path": None,
        }

    git(integration.workspace_path, ["merge", "--abort"], check=False)
    summary_path = persist_merge_conflict_summary(
        project_root,
        run_id,
        bundle_id=bundle_id,
        task_id=task_id,
        branch_name=branch_name,
        integration=integration,
        stderr=completed.stderr,
        stdout=completed.stdout,
    )
    return {
        "status": "conflict",
        "branch_name": branch_name,
        "workspace_path": str(integration.workspace_path.relative_to(project_root)),
        "conflict_summary_path": str(summary_path.relative_to(project_root)),
    }


def cleanup_phase_task_worktrees(project_root: Path, run_id: str, phase_task_ids: list[str]) -> None:
    repo_root = git_root(project_root)
    for task_id in phase_task_ids:
        workspace_path = task_workspace_path(project_root, run_id, task_id)
        branch_name = task_branch_name(run_id, task_id)
        if workspace_path.exists():
            git(repo_root, ["worktree", "remove", "--force", str(workspace_path)], check=False)
        if branch_exists(repo_root, branch_name):
            git(repo_root, ["branch", "-D", branch_name], check=False)


def ensure_worktree(repo_root: Path, branch_name: str, workspace_path: Path) -> None:
    ensure_dir(workspace_path.parent)
    if workspace_path.exists():
        return
    git(repo_root, ["worktree", "add", str(workspace_path), branch_name])


def branch_exists(repo_root: Path, branch_name: str) -> bool:
    completed = git(repo_root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], check=False)
    return completed.returncode == 0


def git(cwd: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise WorktreeError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed


def persist_merge_conflict_summary(
    project_root: Path,
    run_id: str,
    *,
    bundle_id: str,
    task_id: str,
    branch_name: str,
    integration: WorkspaceInfo,
    stderr: str,
    stdout: str,
) -> Path:
    conflicts_dir = ensure_dir(project_root / "runs" / run_id / "merge-conflicts")
    path = conflicts_dir / f"{bundle_id}-{task_id}.json"
    payload = {
        "run_id": run_id,
        "bundle_id": bundle_id,
        "task_id": task_id,
        "branch_name": branch_name,
        "integration_branch": integration.branch_name,
        "integration_workspace": str(integration.workspace_path.relative_to(project_root)),
        "stdout": stdout,
        "stderr": stderr,
    }
    write_json(path, payload)
    return path
