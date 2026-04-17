from __future__ import annotations

from pathlib import Path


def test_server_list_skills_tool_returns_structured_summary(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    workspace_root = tmp_path / "workspace"
    project_skill = workspace_root / ".agents" / "skills" / "project-helper" / "SKILL.md"
    project_skill.parent.mkdir(parents=True, exist_ok=True)
    project_skill.write_text(
        "\n".join(
            [
                "---",
                "name: project-helper",
                "description: Project scoped helper",
                "---",
            ]
        ),
        encoding="utf-8",
    )

    server.WORKSPACE_ROOT = workspace_root
    result = server.list_skills(include_global=False)

    assert result["success"] is True
    assert result["skills"] == [
        {
            "name": "project-helper",
            "description": "Project scoped helper",
            "preferred_path": str(project_skill),
            "sources": [
                {
                    "scope": "project",
                    "namespace": "agents",
                    "path": str(project_skill),
                }
            ],
        }
    ]
