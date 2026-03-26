from __future__ import annotations

from pathlib import Path


SHARED_WORKSPACE_MANIFEST_FILES = [
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
]


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


def app_shared_workspace_ownership(project_root: Path, app_root: Path | None) -> list[dict[str, str]]:
    if app_root is None:
        return []
    app_name = app_root.name
    app_prefix = f"apps/{app_name}"
    entries: list[dict[str, str]] = []
    for filename in SHARED_WORKSPACE_MANIFEST_FILES:
        app_candidate = app_root / filename
        project_candidate = project_root / filename
        if app_candidate.exists():
            path = f"{app_prefix}/{filename}"
        elif project_candidate.exists():
            path = filename
        else:
            continue
        entries.append(
            {
                "path": path,
                "owner_capability": "middleware",
                "reason": "shared app workspace manifest" if filename == "package.json" else "shared app workspace lockfile",
            }
        )
    if (app_root / "scripts").exists():
        entries.append(
            {
                "path": f"{app_prefix}/scripts/**",
                "owner_capability": "middleware",
                "reason": "shared app runtime and tooling scripts",
            }
        )
    return entries


def capability_owned_shared_workspace_paths(
    project_root: Path,
    app_root: Path | None,
    capability: str,
) -> list[str]:
    return [
        entry["path"]
        for entry in app_shared_workspace_ownership(project_root, app_root)
        if entry.get("owner_capability") == capability
    ]


def capability_owned_path_hints(
    project_root: Path,
    objective_id: str,
    capability: str,
    *,
    phase: str | None = None,
) -> list[str]:
    app_root = find_objective_app_root(project_root, objective_id)
    objective_root = find_objective_root(project_root, objective_id)
    hints: list[str] = []
    if app_root is not None:
        app_name = app_root.name
        capability_root = capability_workspace_root(app_root, capability, phase=phase)
        if capability_root is not None and capability_root.exists():
            hints.extend(discover_existing_scope_hints(project_root, capability_root))
        else:
            capability_dirs = {
                "frontend": [f"apps/{app_name}/frontend/**"],
                "backend": [f"apps/{app_name}/backend/**"],
                "middleware": [],
                "shared-platform": [f"apps/{app_name}/shared/**"],
                "documentation": [f"apps/{app_name}/docs/**"],
                "qa": [f"apps/{app_name}/**/*.test.js", f"apps/{app_name}/**/*.spec.js"],
            }
            hints.extend(capability_dirs.get(capability, []))
        if capability in {"documentation", "general", "qa"}:
            objective_docs = app_root / "docs" / "objectives" / objective_id
            if objective_docs.exists():
                hints.extend(discover_existing_scope_hints(project_root, objective_docs))
            else:
                hints.append(f"apps/{app_name}/docs/objectives/{objective_id}/**")
            objective_roles = app_root / "orchestrator" / "roles" / "objectives" / objective_id
            if objective_roles.exists():
                hints.extend(discover_existing_scope_hints(project_root, objective_roles))
            else:
                hints.append(f"apps/{app_name}/orchestrator/roles/objectives/{objective_id}/**")
    if not hints:
        if objective_root.exists():
            hints.extend(discover_existing_scope_hints(project_root, objective_root))
        else:
            try:
                hints.append(str(objective_root.resolve().relative_to(project_root.resolve())))
            except ValueError:
                hints.append(f"orchestrator/roles/objectives/{objective_id}/**")
    return dedupe_strings(hints)


def capability_shared_asset_hints(objective_id: str, capability: str) -> list[str]:
    shared = [f"{objective_id}:{capability}:handoff"]
    if capability == "backend":
        shared.append(f"{objective_id}:api-contract")
    if capability in {"middleware", "shared-platform"}:
        shared.append(f"{objective_id}:integration-contract")
    if capability == "documentation":
        shared.append(f"{objective_id}:release-handoff")
    return dedupe_strings(shared)


def capability_workspace_root(app_root: Path, capability: str, *, phase: str | None = None) -> Path | None:
    if capability == "middleware" and phase == "mvp-build":
        return None
    mapping = {
        "frontend": app_root / "frontend",
        "backend": app_root / "backend",
        "middleware": app_root / "runtime",
        "shared-platform": app_root / "shared",
        "documentation": app_root / "docs",
        "qa": app_root,
    }
    return mapping.get(capability)


def discover_existing_scope_hints(project_root: Path, root: Path, *, max_children: int = 8) -> list[str]:
    if not root.exists():
        return []
    resolved_project_root = project_root.resolve()
    resolved_root = root.resolve()
    relative_root = resolved_root.relative_to(resolved_project_root)
    hints = [f"{relative_root}/**"]
    children = sorted(
        child for child in root.iterdir() if not child.name.startswith(".")
    )
    for child in children[:max_children]:
        child_relative = child.resolve().relative_to(resolved_project_root)
        if child.is_dir():
            hints.append(f"{child_relative}/**")
            if child.name == "src":
                hints.extend(discover_nested_source_hints(project_root, child))
        else:
            if child.suffix in {".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".css"}:
                hints.append(str(child_relative))
    return dedupe_strings(hints)


def discover_nested_source_hints(project_root: Path, source_root: Path, *, max_children: int = 8) -> list[str]:
    hints: list[str] = []
    resolved_project_root = project_root.resolve()
    children = sorted(
        child for child in source_root.iterdir() if not child.name.startswith(".")
    )
    for child in children[:max_children]:
        child_relative = child.resolve().relative_to(resolved_project_root)
        if child.is_dir():
            hints.append(f"{child_relative}/**")
        else:
            if child.suffix in {".js", ".jsx", ".ts", ".tsx", ".json", ".css"}:
                hints.append(str(child_relative))
    return dedupe_strings(hints)


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
