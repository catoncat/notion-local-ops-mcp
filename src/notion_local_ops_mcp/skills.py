from __future__ import annotations

from pathlib import Path


def _read_skill_summary(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {
            "name": path.parent.name,
            "description": "",
        }

    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")

    return {
        "name": metadata.get("name", path.parent.name),
        "description": metadata.get("description", ""),
    }


def _iter_skill_roots(
    workspace_root: Path,
    home_dir: Path,
    *,
    include_project: bool,
    include_global: bool,
) -> list[tuple[str, str, Path]]:
    roots: list[tuple[str, str, Path]] = []
    if include_project:
        roots.extend(
            [
                ("project", "agents", workspace_root / ".agents" / "skills"),
                ("project", "codex", workspace_root / ".codex" / "skills"),
            ]
        )
    if include_global:
        roots.extend(
            [
                ("global", "agents", home_dir / ".agents" / "skills"),
                ("global", "codex", home_dir / ".codex" / "skills"),
            ]
        )
    return roots


def list_skills(
    *,
    workspace_root: Path,
    home_dir: Path | None = None,
    include_project: bool = True,
    include_global: bool = True,
) -> dict[str, object]:
    resolved_workspace = workspace_root.expanduser().resolve()
    resolved_home = (home_dir or Path.home()).expanduser().resolve()
    scanned_roots: list[dict[str, object]] = []
    skills_by_name: dict[str, dict[str, object]] = {}

    for scope, namespace, root in _iter_skill_roots(
        resolved_workspace,
        resolved_home,
        include_project=include_project,
        include_global=include_global,
    ):
        exists = root.exists() and root.is_dir()
        scanned_roots.append(
            {
                "scope": scope,
                "namespace": namespace,
                "path": str(root),
                "exists": exists,
            }
        )
        if not exists:
            continue

        for skill_file in sorted(root.rglob("SKILL.md"), key=lambda item: str(item)):
            summary = _read_skill_summary(skill_file)
            source = {
                "scope": scope,
                "namespace": namespace,
                "path": str(skill_file),
            }
            existing = skills_by_name.get(summary["name"])
            if existing is None:
                skills_by_name[summary["name"]] = {
                    "name": summary["name"],
                    "description": summary["description"],
                    "preferred_path": str(skill_file),
                    "sources": [source],
                }
                continue
            existing["sources"].append(source)

    skills = [skills_by_name[name] for name in sorted(skills_by_name)]
    return {
        "success": True,
        "workspace_root": str(resolved_workspace),
        "scanned_roots": scanned_roots,
        "skills": skills,
    }
