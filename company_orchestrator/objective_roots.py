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


def find_objective_app_root(project_root: Path, objective_id: str) -> Path | None:
    objective_root = find_objective_root(project_root, objective_id)
    apps_root = (project_root / "apps").resolve()
    try:
        relative = objective_root.resolve().relative_to(apps_root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    return apps_root / relative.parts[0]


def capability_owned_path_hints(project_root: Path, objective_id: str, capability: str) -> list[str]:
    app_root = find_objective_app_root(project_root, objective_id)
    hints: list[str] = []
    if app_root is not None:
        app_name = app_root.name
        capability_dirs = {
            "frontend": [f"apps/{app_name}/frontend/**"],
            "backend": [f"apps/{app_name}/backend/**"],
            "middleware": [f"apps/{app_name}/runtime/**"],
            "shared-platform": [f"apps/{app_name}/shared/**"],
            "documentation": [f"apps/{app_name}/docs/**"],
            "qa": [f"apps/{app_name}/**/*.test.js", f"apps/{app_name}/**/*.spec.js"],
        }
        hints.extend(capability_dirs.get(capability, []))
        hints.append(f"apps/{app_name}/docs/objectives/{objective_id}/**")
        hints.append(f"apps/{app_name}/orchestrator/roles/objectives/{objective_id}/**")
    if not hints:
        hints.append(f"docs/objectives/{objective_id}/**")
    return dedupe_strings(hints)


def capability_shared_asset_hints(objective_id: str, capability: str) -> list[str]:
    shared = [f"{objective_id}:{capability}:handoff"]
    if capability in {"frontend", "backend", "middleware"}:
        shared.append(f"{objective_id}:api-contract")
    if capability in {"middleware", "shared-platform"}:
        shared.append(f"{objective_id}:integration-contract")
    if capability == "documentation":
        shared.append(f"{objective_id}:release-handoff")
    return dedupe_strings(shared)


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
