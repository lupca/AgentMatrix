from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from app.db.base import get_db
from app.db.models import Agent as AgentModel
from app.graph.context import invalidate_context_snapshot
from app.schemas.agent import Agent, AgentCreate, AgentUpdate
from app.services import entity_admin
from app.db.archive import with_archived
from app.services.archive import ArchiveError, ArchiveService

router = APIRouter(prefix="/api/agents", tags=["agents"])


@router.get("", response_model=list[Agent])
def get_agents(
    role: str | None = None,
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_archived: bool = Query(False),
    db: Session = Depends(get_db)
):
    query = with_archived(db, AgentModel, include_archived)
    if role:
        query = query.filter(AgentModel.role == role)
    if status:
        query = query.filter(AgentModel.status == status)
    return query.offset(offset).limit(limit).all()


@router.post("", response_model=Agent, status_code=status.HTTP_201_CREATED)
def create_agent(agent_in: AgentCreate, db: Session = Depends(get_db)):
    agent_data = agent_in.model_dump(exclude_unset=True)
    try:
        return entity_admin.create_agent(db, agent_data)
    except entity_admin.EntityConflictError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except entity_admin.EntityValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )


@router.get("/{id}", response_model=Agent)
def get_agent(id: str, include_archived: bool = Query(False), db: Session = Depends(get_db)):
    db_agent = with_archived(db, AgentModel, include_archived).filter(AgentModel.id == id).first()
    if not db_agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{id}' not found."
        )
    return db_agent


@router.patch("/{id}", response_model=Agent)
def update_agent(id: str, agent_in: AgentUpdate, db: Session = Depends(get_db)):
    update_data = agent_in.model_dump(exclude_unset=True)
    try:
        return entity_admin.update_agent(db, id, update_data)
    except entity_admin.EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except entity_admin.EntityValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )


@router.post("/{id}/set-default", response_model=Agent)
def set_default_agent(id: str, db: Session = Depends(get_db)):
    db_agent = db.query(AgentModel).filter(AgentModel.id == id).first()
    if not db_agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{id}' not found.",
        )
    if db_agent.role != "coordinator":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only coordinator agents can be the default coordinator.",
        )

    entity_admin.unset_coordinator_defaults(db, except_id=id)
    db_agent.is_default = True
    db.commit()
    db.refresh(db_agent)
    invalidate_context_snapshot(db)
    return db_agent


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(id: str, db: Session = Depends(get_db)):
    try:
        ArchiveService(db, "rest:agents").archive("agents", id)
    except ArchiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    invalidate_context_snapshot(db)
    return None


@router.post("/{id}/archive")
def archive_agent(id: str, db: Session = Depends(get_db)):
    try:
        return ArchiveService(db, "rest:agents").archive("agents", id)
    except ArchiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{id}/restore")
def restore_agent(id: str, db: Session = Depends(get_db)):
    try:
        return ArchiveService(db, "rest:agents").restore("agents", id)
    except ArchiveError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
