from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import ContextLevel, Project as ProjectModel, Session as SessionModel
from app.graph.context import invalidate_context_snapshot
from app.schemas.project import Project, ProjectCreate, ProjectUpdate

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[Project])
def get_projects(
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db)
):
    query = db.query(ProjectModel)
    if status:
        query = query.filter(ProjectModel.status == status)
    return query.offset(offset).limit(limit).all()


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
def create_project(project_in: ProjectCreate, db: Session = Depends(get_db)):
    existing = db.query(ProjectModel).filter(ProjectModel.id == project_in.id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Project with ID '{project_in.id}' already exists."
        )

    project_data = project_in.model_dump(exclude_unset=True)
    db_project = ProjectModel(**project_data)
    db.add(db_project)
    db.commit()
    db.refresh(db_project)
    invalidate_context_snapshot(db, project_id=db_project.id)
    return db_project


@router.get("/{id}", response_model=Project)
def get_project(id: str, db: Session = Depends(get_db)):
    db_project = db.query(ProjectModel).filter(ProjectModel.id == id).first()
    if not db_project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{id}' not found."
        )
    return db_project


@router.patch("/{id}", response_model=Project)
def update_project(id: str, project_in: ProjectUpdate, db: Session = Depends(get_db)):
    db_project = db.query(ProjectModel).filter(ProjectModel.id == id).first()
    if not db_project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{id}' not found."
        )

    update_data = project_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_project, field, value)

    db.commit()
    db.refresh(db_project)
    invalidate_context_snapshot(db, project_id=db_project.id)
    return db_project


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(id: str, db: Session = Depends(get_db)):
    db_project = db.query(ProjectModel).filter(ProjectModel.id == id).first()
    if not db_project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{id}' not found."
        )

    # Sessions scoped to this project would otherwise violate
    # ck_sessions_context_level_consistency once project_id is nulled out by
    # the FK's ON DELETE SET NULL, so demote them to global context first.
    db.query(SessionModel).filter(SessionModel.project_id == id).update(
        {
            SessionModel.context_level: ContextLevel.GLOBAL.value,
            SessionModel.project_id: None,
            SessionModel.task_id: None,
        },
        synchronize_session=False,
    )

    db.delete(db_project)
    db.commit()
    invalidate_context_snapshot(db, project_id=id)
    return None


@router.post("/{id}/build-graph")
@router.post('/api/projects/{id}/build-graph')
def build_graph(id: str):
    # Placeholder for graph building
    return {'status': 'building', 'project_id': id}
