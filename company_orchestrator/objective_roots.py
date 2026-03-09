from __future__ import annotations

from pathlib import Path


def find_objective_root(project_root: Path, objective_id: str, *, create: bool = False) -> Path:
    generic_root = project_root / "orchestrator" / "roles" / "objectives" / objective_id
    if generic_root.exists():
        return generic_root

    apps_root = project_root / "apps"
    if apps_root.exists():
        for app_root in sorted(path for path in apps_root.iterdir() if path.is_dir()):
            candidate = app_root / "orchestrator" / "roles" / "objectives" / objective_id
            if candidate.exists():
                return candidate

    if create:
        return generic_root

    return generic_root
