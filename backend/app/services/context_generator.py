"""Context generator service for checking and injecting project context & scoped rules."""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any
from sqlalchemy.orm import Session

from app.db.models import Project, ProjectRule


CONTEXT_GEN_PROMPT = """You are generating project context for an AI coding assistant system.

Scan this repository and then call the `save_project_context` tool with:

1. **context_md** (≤150 lines total):
```markdown
# Project: {project_name}

## Stack
One line describing tech stack (e.g., "FastAPI + SQLAlchemy + React")

## Hard Boundaries (max 7 rules)
- Critical rules that MUST NOT be violated
- Example: "NEVER use db.commit() directly, use db.flush() + db.refresh()"

## Key Patterns (max 5)
- Common patterns in this codebase
- Example: "Routes → Services → Repositories pattern"
```

2. **rules** (max 5 scoped rules):
Each rule should have:
- name: short identifier (e.g., "architecture", "schemas", "api")
- globs: file patterns it applies to (e.g., ["backend/app/schemas/**/*.py"])
- content: the rule details (≤30 lines)

IMPORTANT:
- Keep EVERYTHING concise - bloated context hurts agent performance
- Do NOT document file structure (gets stale quickly)
- Focus on conventions and constraints, not descriptions
- Hard boundaries = things that will break if violated

After scanning, call `save_project_context` with task_id="{task_id}" and project_id="{project_id}".
"""


class ContextChecker:
    """Check if project has context ready, used before dispatch."""

    def __init__(self, db: Session):
        self.db = db

    def check_project_ready(self, project_id: str) -> dict[str, Any]:
        """Check if project has required context."""
        project = self.db.get(Project, project_id)
        if not project:
            return {"exists": False, "ready": False}

        rules = self.db.query(ProjectRule).filter_by(project_id=project_id).all()
        has_context = bool(project.context_md and project.context_md.strip())
        has_rules = len(rules) > 0
        context_generated = bool(getattr(project, "context_generated", False))

        return {
            "exists": True,
            "has_context": has_context,
            "has_rules": has_rules,
            "context_generated": context_generated,
            "ready": has_context and has_rules,
        }


def get_matching_rules(
    db: Session,
    project_id: str,
    task_files: list[str] | None,
) -> list[ProjectRule]:
    """Get rules matching task files using glob patterns."""
    rules = (
        db.query(ProjectRule)
        .filter(ProjectRule.project_id == project_id)
        .order_by(ProjectRule.priority.desc())
        .all()
    )

    if not task_files:
        # No files specified -> return all rules
        return rules

    matched: list[ProjectRule] = []
    for rule in rules:
        if not rule.globs:
            # No globs = applies to all files
            matched.append(rule)
            continue

        for task_file in task_files:
            if any(fnmatch(task_file, glob) for glob in rule.globs):
                matched.append(rule)
                break

    return matched
