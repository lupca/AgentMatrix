"""API endpoints for managing scoped project rules."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.db.models import Project as ProjectModel, ProjectRule
from app.schemas.project_rule import (
    ProjectRuleCreate,
    ProjectRuleRead,
    ProjectRuleUpdate,
)

router = APIRouter(prefix="/api/projects/{project_id}/rules", tags=["project-rules"])


@router.get("", response_model=list[ProjectRuleRead])
def list_rules(project_id: str, db: Session = Depends(get_db)):
    project = db.get(ProjectModel, project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_id}' not found.",
        )
    return (
        db.query(ProjectRule)
        .filter_by(project_id=project_id)
        .order_by(ProjectRule.priority.desc())
        .all()
    )


@router.post("", response_model=ProjectRuleRead, status_code=status.HTTP_201_CREATED)
def create_rule(
    project_id: str,
    data: ProjectRuleCreate,
    db: Session = Depends(get_db),
):
    project = db.get(ProjectModel, project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{project_id}' not found.",
        )

    rule = ProjectRule(
        project_id=project_id,
        **data.model_dump(),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.put("/{rule_id}", response_model=ProjectRuleRead)
def update_rule(
    project_id: str,
    rule_id: str,
    data: ProjectRuleUpdate,
    db: Session = Depends(get_db),
):
    rule = db.query(ProjectRule).filter_by(id=rule_id, project_id=project_id).first()
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule '{rule_id}' not found for project '{project_id}'.",
        )

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)

    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(
    project_id: str,
    rule_id: str,
    db: Session = Depends(get_db),
):
    rule = db.query(ProjectRule).filter_by(id=rule_id, project_id=project_id).first()
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule '{rule_id}' not found for project '{project_id}'.",
        )

    db.delete(rule)
    db.commit()
    return None
