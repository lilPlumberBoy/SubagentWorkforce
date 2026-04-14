from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
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


_WORKTREE_MUTATION_LOCK = threading.RLock()
_PATHSPEC_BATCH_SIZE = 200


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
    with _WORKTREE_MUTATION_LOCK:
        ensure_branch(repo_root, branch_name, "HEAD")
        ensure_worktree(repo_root, branch_name, workspace_path)
    return WorkspaceInfo(branch_name=branch_name, workspace_path=workspace_path)


def ensure_task_workspace(project_root: Path, run_id: str, task_id: str) -> WorkspaceInfo:
    return ensure_task_workspace_with_refresh(project_root, run_id, task_id, refresh=False)


def ensure_task_workspace_with_refresh(project_root: Path, run_id: str, task_id: str, *, refresh: bool) -> WorkspaceInfo:
    repo_root = git_root(project_root)
    branch_name = task_branch_name(run_id, task_id)
    workspace_path = task_workspace_path(project_root, run_id, task_id)
    if refresh:
        return recreate_task_workspace(project_root, run_id, task_id)
    with _WORKTREE_MUTATION_LOCK:
        integration = ensure_run_integration_workspace(project_root, run_id)
        ensure_branch(repo_root, branch_name, integration.branch_name)
        ensure_worktree(repo_root, branch_name, workspace_path)
    return WorkspaceInfo(branch_name=branch_name, workspace_path=workspace_path)


def recreate_task_workspace(project_root: Path, run_id: str, task_id: str) -> WorkspaceInfo:
    repo_root = git_root(project_root)
    branch_name = task_branch_name(run_id, task_id)
    workspace_path = task_workspace_path(project_root, run_id, task_id)
    with _WORKTREE_MUTATION_LOCK:
        integration = ensure_run_integration_workspace(project_root, run_id)
        if workspace_path.exists():
            git(repo_root, ["worktree", "remove", "--force", str(workspace_path)], check=False)
        if branch_exists(repo_root, branch_name):
            git(repo_root, ["branch", "-D", branch_name], check=False)
        ensure_branch(repo_root, branch_name, integration.branch_name)
        ensure_worktree(repo_root, branch_name, workspace_path)
    return WorkspaceInfo(branch_name=branch_name, workspace_path=workspace_path)


def commit_task_workspace(
    workspace: WorkspaceInfo,
    task_id: str,
    *,
    allowed_paths: list[str] | None = None,
) -> dict[str, Any]:
    normalized_allowed_paths = [
        normalize_repo_relative_path(value)
        for value in (allowed_paths or [])
        if normalize_repo_relative_path(value)
    ]
    discarded_paths: list[str] = []
    if normalized_allowed_paths:
        repo_root = git_root(workspace.workspace_path)
        discarded_paths = discard_pending_workspace_changes(
            repo_root,
            workspace.workspace_path,
            allowed_paths=normalized_allowed_paths,
            ref_name="HEAD",
        )
    git(workspace.workspace_path, ["add", "-A"])
    status = git(workspace.workspace_path, ["status", "--porcelain"])
    if not status.stdout.strip():
        return {"committed": False, "commit_sha": None, "discarded_paths": discarded_paths}
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
    return {"committed": True, "commit_sha": head, "discarded_paths": discarded_paths}


def merge_task_branch(
    project_root: Path,
    run_id: str,
    task_id: str,
    *,
    bundle_id: str,
    allowed_paths: list[str] | None = None,
) -> dict[str, Any]:
    with _WORKTREE_MUTATION_LOCK:
        integration = ensure_run_integration_workspace(project_root, run_id)
        branch_name = task_branch_name(run_id, task_id)
        integration_sanitize_result = sanitize_integration_workspace_for_merge(
            project_root,
            run_id,
            integration_branch=integration.branch_name,
            allowed_paths=allowed_paths or [],
        )
        sanitize_result = sanitize_task_branch_for_landing(
            project_root,
            run_id,
            task_id,
            branch_name=branch_name,
            integration_branch=integration.branch_name,
            allowed_paths=allowed_paths or [],
        )
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
            "discarded_paths": sorted(
                set(integration_sanitize_result["discarded_paths"] + sanitize_result["discarded_paths"])
            ),
            "sanitized_commit_sha": sanitize_result["sanitized_commit_sha"],
            "integration_sanitized_paths": integration_sanitize_result["discarded_paths"],
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
        "discarded_paths": sorted(
            set(integration_sanitize_result["discarded_paths"] + sanitize_result["discarded_paths"])
        ),
        "sanitized_commit_sha": sanitize_result["sanitized_commit_sha"],
        "integration_sanitized_paths": integration_sanitize_result["discarded_paths"],
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


def ensure_branch(repo_root: Path, branch_name: str, start_point: str) -> None:
    if branch_exists(repo_root, branch_name):
        return
    completed = git(repo_root, ["branch", branch_name, start_point], check=False)
    if completed.returncode == 0 or branch_exists(repo_root, branch_name):
        return
    raise WorktreeError(completed.stderr.strip() or f"git branch {branch_name} {start_point} failed")


def ensure_worktree(repo_root: Path, branch_name: str, workspace_path: Path) -> None:
    ensure_dir(workspace_path.parent)
    if workspace_path.exists():
        return
    completed = git(repo_root, ["worktree", "add", str(workspace_path), branch_name], check=False)
    if completed.returncode == 0 or workspace_path.exists():
        return
    raise WorktreeError(completed.stderr.strip() or f"git worktree add {workspace_path} {branch_name} failed")


def branch_exists(repo_root: Path, branch_name: str) -> bool:
    completed = git(repo_root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"], check=False)
    return completed.returncode == 0


def normalize_repo_relative_path(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def path_matches_allowed_surface(path_value: str, allowed_paths: list[str]) -> bool:
    normalized_path = normalize_repo_relative_path(path_value)
    if not normalized_path:
        return False
    for raw_allowed in allowed_paths:
        allowed = normalize_repo_relative_path(raw_allowed)
        if not allowed:
            continue
        if allowed.endswith("/**"):
            prefix = allowed[:-3].rstrip("/")
            if normalized_path == prefix or normalized_path.startswith(f"{prefix}/"):
                return True
            continue
        if allowed.endswith("/"):
            prefix = allowed.rstrip("/")
            if normalized_path == prefix or normalized_path.startswith(f"{prefix}/"):
                return True
            continue
        if normalized_path == allowed:
            return True
    return False


def workspace_status_paths(workspace_path: Path) -> list[str]:
    status = git(workspace_path, ["status", "--porcelain", "--untracked-files=all"]).stdout.splitlines()
    paths: list[str] = []
    seen: set[str] = set()
    for line in status:
        if len(line) < 4:
            continue
        raw_path = line[3:]
        if " -> " in raw_path:
            old_path, new_path = raw_path.split(" -> ", 1)
            for value in (old_path, new_path):
                normalized = normalize_repo_relative_path(value.strip('"'))
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    paths.append(normalized)
            continue
        normalized = normalize_repo_relative_path(raw_path.strip('"'))
        if normalized and normalized not in seen:
            seen.add(normalized)
            paths.append(normalized)
    return paths


def batched_pathspecs(paths: list[str], *, size: int = _PATHSPEC_BATCH_SIZE) -> list[list[str]]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in paths:
        candidate = normalize_repo_relative_path(value)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return [normalized[index : index + size] for index in range(0, len(normalized), size)]


def ref_existing_paths(repo_root: Path, ref_name: str, paths: list[str]) -> set[str]:
    existing: set[str] = set()
    for chunk in batched_pathspecs(paths):
        completed = git(repo_root, ["ls-tree", "-r", "--name-only", ref_name, "--", *chunk], check=False)
        if completed.returncode != 0:
            continue
        for raw_path in completed.stdout.splitlines():
            normalized = normalize_repo_relative_path(raw_path)
            if normalized:
                existing.add(normalized)
    return existing


def remove_workspace_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink()


def restore_paths_from_ref(repo_root: Path, workspace_path: Path, ref_name: str, paths: list[str]) -> None:
    normalized_paths = [normalize_repo_relative_path(path_value) for path_value in paths if normalize_repo_relative_path(path_value)]
    if not normalized_paths:
        return
    existing_paths = ref_existing_paths(repo_root, ref_name, normalized_paths)
    paths_to_restore = [path_value for path_value in normalized_paths if path_value in existing_paths]
    paths_to_remove = [path_value for path_value in normalized_paths if path_value not in existing_paths]
    for chunk in batched_pathspecs(paths_to_restore):
        git(
            workspace_path,
            ["restore", "--source", ref_name, "--staged", "--worktree", "--", *chunk],
            check=False,
        )
    for chunk in batched_pathspecs(paths_to_remove):
        git(workspace_path, ["rm", "-f", "--ignore-unmatch", "--", *chunk], check=False)
    for chunk in batched_pathspecs(paths_to_remove):
        git(workspace_path, ["clean", "-fd", "--", *chunk], check=False)
    for path_value in paths_to_remove:
        candidate = workspace_path / path_value
        if candidate.exists():
            remove_workspace_path(candidate)


def discard_pending_workspace_changes(
    repo_root: Path,
    workspace_path: Path,
    *,
    allowed_paths: list[str],
    ref_name: str,
) -> list[str]:
    disallowed_paths = [
        path_value
        for path_value in workspace_status_paths(workspace_path)
        if not path_matches_allowed_surface(path_value, allowed_paths)
    ]
    restore_paths_from_ref(repo_root, workspace_path, ref_name, disallowed_paths)
    return disallowed_paths


def branch_unique_paths(repo_root: Path, integration_branch: str, branch_name: str) -> list[str]:
    completed = git(repo_root, ["diff", "--name-only", f"{integration_branch}...{branch_name}"])
    paths: list[str] = []
    seen: set[str] = set()
    for raw_path in completed.stdout.splitlines():
        normalized = normalize_repo_relative_path(raw_path)
        if normalized and normalized not in seen:
            seen.add(normalized)
            paths.append(normalized)
    return paths


def sanitize_task_branch_for_landing(
    project_root: Path,
    run_id: str,
    task_id: str,
    *,
    branch_name: str,
    integration_branch: str,
    allowed_paths: list[str],
) -> dict[str, Any]:
    normalized_allowed = [normalize_repo_relative_path(value) for value in allowed_paths if normalize_repo_relative_path(value)]
    if not normalized_allowed:
        return {"discarded_paths": [], "sanitized_commit_sha": None}
    repo_root = git_root(project_root)
    workspace_path = task_workspace_path(project_root, run_id, task_id)
    ensure_worktree(repo_root, branch_name, workspace_path)
    discarded_paths = discard_pending_workspace_changes(
        repo_root,
        workspace_path,
        allowed_paths=normalized_allowed,
        ref_name="HEAD",
    )
    disallowed_branch_paths = [
        path_value
        for path_value in branch_unique_paths(repo_root, integration_branch, branch_name)
        if not path_matches_allowed_surface(path_value, normalized_allowed)
    ]
    restore_paths_from_ref(repo_root, workspace_path, integration_branch, disallowed_branch_paths)
    status = git(workspace_path, ["status", "--porcelain"])
    sanitized_commit_sha = None
    if status.stdout.strip():
        git(workspace_path, ["add", "-A"])
        git(
            workspace_path,
            [
                "-c",
                "user.name=company-orchestrator",
                "-c",
                "user.email=company-orchestrator@example.invalid",
                "commit",
                "-m",
                f"orchestrator: sanitize {task_id} landing",
            ],
        )
        sanitized_commit_sha = git(workspace_path, ["rev-parse", "HEAD"]).stdout.strip()
    return {
        "discarded_paths": sorted(set(discarded_paths + disallowed_branch_paths)),
        "sanitized_commit_sha": sanitized_commit_sha,
    }


def sanitize_integration_workspace_for_merge(
    project_root: Path,
    run_id: str,
    *,
    integration_branch: str,
    allowed_paths: list[str],
) -> dict[str, Any]:
    normalized_allowed = [normalize_repo_relative_path(value) for value in allowed_paths if normalize_repo_relative_path(value)]
    if not normalized_allowed:
        return {"discarded_paths": []}
    repo_root = git_root(project_root)
    workspace_path = integration_workspace_path(project_root, run_id)
    ensure_worktree(repo_root, integration_branch, workspace_path)
    overlapping_paths = [
        path_value
        for path_value in workspace_status_paths(workspace_path)
        if path_matches_allowed_surface(path_value, normalized_allowed)
    ]
    restore_paths_from_ref(repo_root, workspace_path, integration_branch, overlapping_paths)
    return {
        "discarded_paths": sorted(set(overlapping_paths)),
    }


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
