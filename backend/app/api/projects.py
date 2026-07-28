from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import ContextLevel, Project as ProjectModel, Session as SessionModel
from app.graph.context import invalidate_context_snapshot
from app.schemas.project import Project, ProjectCreate, ProjectUpdate
from app.services import entity_admin
from app.db.archive import with_archived
from app.services.archive import ArchiveError, ArchiveService

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[Project])
def get_projects(
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(False),
    db: Session = Depends(get_db)
):
    query = with_archived(db, ProjectModel, include_archived)
    if status:
        query = query.filter(ProjectModel.status == status)
    return query.offset(offset).limit(limit).all()


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
def create_project(project_in: ProjectCreate, db: Session = Depends(get_db)):
    project_data = project_in.model_dump(exclude_unset=True)
    try:
        return entity_admin.create_project(db, project_data)
    except entity_admin.EntityConflictError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except entity_admin.EntityValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )


@router.get("/{id}", response_model=Project)
def get_project(id: str, include_archived: bool = Query(False), db: Session = Depends(get_db)):
    db_project = with_archived(db, ProjectModel, include_archived).filter(ProjectModel.id == id).first()
    if not db_project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project '{id}' not found."
        )
    return db_project


@router.patch("/{id}", response_model=Project)
def update_project(id: str, project_in: ProjectUpdate, db: Session = Depends(get_db)):
    update_data = project_in.model_dump(exclude_unset=True)
    try:
        return entity_admin.update_project(db, id, update_data)
    except entity_admin.EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(id: str, db: Session = Depends(get_db)):
    try:
        ArchiveService(db, "rest:projects").archive_project(id)
    except ArchiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return None


@router.post("/{id}/archive")
def archive_project(id: str, db: Session = Depends(get_db)):
    try:
        return ArchiveService(db, "rest:projects").archive_project(id)
    except ArchiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{id}/restore")
def restore_project(id: str, db: Session = Depends(get_db)):
    try:
        return ArchiveService(db, "rest:projects").restore_project(id)
    except ArchiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{id}/build-graph")
@router.post('/api/projects/{id}/build-graph')
def build_graph(id: str):
    # Placeholder for graph building
    return {'status': 'building', 'project_id': id}
