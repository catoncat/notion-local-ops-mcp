from __future__ import annotations

from pathlib import Path

from notion_local_ops_mcp.skills import list_skills
from tests.helpers import normalize_path_slashes


def _write_skill(root: Path, folder: str, *, name: str, description: str) -> None:
    target = root / folder / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "\n".join(
            [
                "---",
                f"name: {name}",
                f"description: {description}",
                "---",
                "",
                f"# {name}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_list_skills_discovers_project_and_global_roots(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    home_dir = tmp_path / "home"

    _write_skill(
        workspace_root / ".agents" / "skills",
        "project-helper",
        name="project-helper",
        description="Project scoped helper",
    )
    _write_skill(
        home_dir / ".agents" / "skills",
        "global-helper",
        name="global-helper",
        description="Global helper",
    )

    result = list_skills(workspace_root=workspace_root, home_dir=home_dir)

    assert result["success"] is True
    assert [skill["name"] for skill in result["skills"]] == ["global-helper", "project-helper"]
    project_skill = result["skills"][1]
    assert normalize_path_slashes(project_skill["preferred_path"]).endswith(
        ".agents/skills/project-helper/SKILL.md"
    )
    assert project_skill["sources"] == [
        {
            "scope": "project",
            "namespace": "agents",
            "path": str(workspace_root / ".agents" / "skills" / "project-helper" / "SKILL.md"),
        }
    ]


def test_list_skills_deduplicates_same_skill_name_and_preserves_sources(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    home_dir = tmp_path / "home"

    _write_skill(
        home_dir / ".agents" / "skills",
        "shared-helper",
        name="shared-helper",
        description="Preferred global helper",
    )
    _write_skill(
        home_dir / ".codex" / "skills",
        "shared-helper",
        name="shared-helper",
        description="Duplicate global helper",
    )

    result = list_skills(workspace_root=workspace_root, home_dir=home_dir)

    assert result["success"] is True
    assert len(result["skills"]) == 1
    assert result["skills"][0]["name"] == "shared-helper"
    assert result["skills"][0]["description"] == "Preferred global helper"
    assert result["skills"][0]["preferred_path"] == str(
        home_dir / ".agents" / "skills" / "shared-helper" / "SKILL.md"
    )
    assert result["skills"][0]["sources"] == [
        {
            "scope": "global",
            "namespace": "agents",
            "path": str(home_dir / ".agents" / "skills" / "shared-helper" / "SKILL.md"),
        },
        {
            "scope": "global",
            "namespace": "codex",
            "path": str(home_dir / ".codex" / "skills" / "shared-helper" / "SKILL.md"),
        },
    ]


def test_list_skills_discovers_global_claude_root_without_hardcoded_username(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    home_dir = tmp_path / "custom-home"

    _write_skill(
        home_dir / ".claude" / "skills",
        "claude-helper",
        name="claude-helper",
        description="Claude scoped helper",
    )

    result = list_skills(workspace_root=workspace_root, home_dir=home_dir)

    assert result["success"] is True
    assert result["skills"] == [
        {
            "name": "claude-helper",
            "description": "Claude scoped helper",
            "preferred_path": str(home_dir / ".claude" / "skills" / "claude-helper" / "SKILL.md"),
            "sources": [
                {
                    "scope": "global",
                    "namespace": "claude",
                    "path": str(
                        home_dir / ".claude" / "skills" / "claude-helper" / "SKILL.md"
                    ),
                }
            ],
        }
    ]
    assert any(
        root["path"] == str(home_dir / ".claude" / "skills") and root["namespace"] == "claude"
        for root in result["scanned_roots"]
    )
